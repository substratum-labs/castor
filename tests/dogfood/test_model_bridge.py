from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import jsonschema
from tests.dogfood.model_bridge import (
    ModelBridgeError,
    ModelRunConfig,
    build_codex_argv,
    build_model_env,
    context_manifest_sha256,
    project_provider_schema,
    run_model,
    run_outer_sandbox_preflight,
)
from tests.dogfood.provider_proxy import (
    ProviderProxyServer,
    ProxyAudit,
    ProxyPolicyError,
    parse_allowed_connect_target,
    validate_redirect_target,
)

DOGFOOD_DIR = Path(__file__).resolve().parent
OUTPUT_SCHEMA = DOGFOOD_DIR / "model_output.schema.json"
OPENAI_SCHEMA = DOGFOOD_DIR / "model_output_openai.schema.json"
RATE_CARD_SCHEMA = DOGFOOD_DIR / "rate_card.schema.json"
RATE_CARD_FIXTURE = DOGFOOD_DIR / "fixtures" / "rate_card_valid.json"


class FakeRunner:
    def __init__(
        self,
        completed: subprocess.CompletedProcess[str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.completed = completed
        self.error = error
        self.calls: list[tuple[list[str], str, int, dict[str, str]]] = []

    def run(
        self,
        argv: list[str],
        stdin_text: str,
        timeout_seconds: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, stdin_text, timeout_seconds, env))
        if self.error is not None:
            raise self.error
        assert self.completed is not None
        return self.completed


class FakePreflightRunner:
    def __init__(self) -> None:
        self.argv: list[str] | None = None

    def __call__(
        self,
        argv: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "arbitrary_egress_denied": True,
                    "curated_read": True,
                    "host_path_denied": True,
                    "schema_read": True,
                }
            )
            + "\n",
            stderr="",
        )


class ModelBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.context = root / "context"
        self.context.mkdir()
        (self.context / "task.txt").write_text("bounded task\n", encoding="utf-8")
        self.rate_card = root / "rate-card.json"
        self.rate_card.write_text(
            RATE_CARD_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.config = ModelRunConfig(
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            context_dir=self.context,
            output_schema=OUTPUT_SCHEMA,
            rate_card_path=self.rate_card,
            cost_limit_usd=Decimal("5.00"),
            provider_proxy_url="http://provider-proxy:8080",
            timeout_seconds=600,
        )

    def completion(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "files": [
                {
                    "path": "src/castor/ipc_client.py",
                    "content_utf8": "from __future__ import annotations\n",
                },
                {
                    "path": "tests/test_ipc_client.py",
                    "content_utf8": "from __future__ import annotations\n",
                },
            ],
            "explanation": "Create a typed client and deterministic tests.",
        }

    def successful_stdout(
        self,
        completion: dict[str, object] | None = None,
        *,
        input_tokens: int = 120,
        cached_input_tokens: int = 20,
        output_tokens: int = 30,
        provider_request_count: int = 1,
    ) -> str:
        records = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": json.dumps(completion or self.completion()),
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                },
            },
            {
                "type": "bridge.audit",
                "cli_version": "codex-cli 0.153.4",
                "provider_request_count": provider_request_count,
            },
        ]
        return "".join(json.dumps(record) + "\n" for record in records)

    def runner_for(
        self, stdout: str | None = None, *, returncode: int = 0, stderr: str = ""
    ) -> FakeRunner:
        return FakeRunner(
            subprocess.CompletedProcess(
                args=build_codex_argv(self.config),
                returncode=returncode,
                stdout=stdout if stdout is not None else self.successful_stdout(),
                stderr=stderr,
            )
        )

    def test_build_codex_argv_is_exact_and_prompt_is_stdin(self) -> None:
        expected = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--model",
            "gpt-5.6-sol",
            "--config",
            'model_reasoning_effort="medium"',
            "-C",
            str(self.context),
            "--skip-git-repo-check",
            "--output-schema",
            str(project_provider_schema(OUTPUT_SCHEMA, "openai")),
            "--json",
            "-",
        ]
        runner = self.runner_for()

        run_model(self.config, "do the bounded task", runner)

        self.assertEqual(
            runner.calls,
            [(expected, "do the bounded task", 600, build_model_env(self.config))],
        )

    def test_model_environment_is_a_minimal_allowlist(self) -> None:
        ambient = {
            "OPENAI_API_KEY": "test-credential",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "http_proxy": "http://attacker.invalid",
            "HTTPS_PROXY": "http://attacker.invalid",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret",
            "GIT_CONFIG_GLOBAL": "/tmp/host-git-config",
            "HOME": "/Users/host",
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            env = build_model_env(self.config)

        self.assertEqual(
            env,
            {
                "OPENAI_API_KEY": "test-credential",
                "HTTP_PROXY": "http://provider-proxy:8080",
                "HTTPS_PROXY": "http://provider-proxy:8080",
            },
        )

    def test_outer_sandbox_preflight_uses_no_network_and_read_only_mounts(self) -> None:
        runner = FakePreflightRunner()

        result = run_outer_sandbox_preflight(runner)

        assert runner.argv is not None
        self.assertEqual(
            result,
            {
                "arbitrary_egress_denied": True,
                "curated_read": True,
                "host_path_denied": True,
                "schema_read": True,
            },
        )
        self.assertIn("--network", runner.argv)
        self.assertEqual(runner.argv[runner.argv.index("--network") + 1], "none")
        self.assertIn("--read-only", runner.argv)
        self.assertIn("--cap-drop", runner.argv)
        self.assertEqual(runner.argv[runner.argv.index("--cap-drop") + 1], "ALL")
        mounts = [
            runner.argv[index + 1]
            for index, argument in enumerate(runner.argv)
            if argument == "--mount"
        ]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all("readonly" in mount for mount in mounts))
        self.assertEqual(
            runner.argv[runner.argv.index("--entrypoint") + 1],
            "node",
        )
        self.assertIn("castor-dogfood-model:t320d", runner.argv)

    def test_output_and_rate_card_schemas_are_closed_and_valid(self) -> None:
        output_schema = json.loads(OUTPUT_SCHEMA.read_text(encoding="utf-8"))
        openai_schema = json.loads(OPENAI_SCHEMA.read_text(encoding="utf-8"))
        rate_schema = json.loads(RATE_CARD_SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(output_schema)
        jsonschema.Draft202012Validator.check_schema(openai_schema)
        jsonschema.Draft202012Validator.check_schema(rate_schema)
        jsonschema.Draft202012Validator(output_schema).validate(self.completion())
        jsonschema.Draft202012Validator(openai_schema).validate(self.completion())
        jsonschema.Draft202012Validator(rate_schema).validate(
            json.loads(RATE_CARD_FIXTURE.read_text(encoding="utf-8"))
        )
        self.assertFalse(output_schema["additionalProperties"])
        self.assertFalse(openai_schema["additionalProperties"])
        self.assertFalse(rate_schema["additionalProperties"])

    def test_rejects_extra_missing_and_out_of_order_paths(self) -> None:
        variants = []
        extra = self.completion()
        extra["files"] = [
            *extra["files"],
            {"path": "pyproject.toml", "content_utf8": "hostile"},
        ]
        variants.append(extra)
        missing = self.completion()
        missing["files"] = missing["files"][:1]
        variants.append(missing)
        reversed_paths = self.completion()
        reversed_paths["files"] = list(reversed(reversed_paths["files"]))
        variants.append(reversed_paths)

        for completion in variants:
            with (
                self.subTest(completion=completion),
                self.assertRaises(ModelBridgeError),
            ):
                run_model(
                    self.config,
                    "prompt",
                    self.runner_for(self.successful_stdout(completion)),
                )

    def test_rejects_utf8_content_over_65536_bytes(self) -> None:
        completion = self.completion()
        completion["files"][0]["content_utf8"] = "é" * 32769

        with self.assertRaisesRegex(ModelBridgeError, "65,536 UTF-8 bytes"):
            run_model(
                self.config,
                "prompt",
                self.runner_for(self.successful_stdout(completion)),
            )

    def test_rejects_missing_explanation_and_unknown_completion_field(self) -> None:
        missing = self.completion()
        del missing["explanation"]
        extra = self.completion()
        extra["unexpected"] = True

        for completion in (missing, extra):
            with (
                self.subTest(completion=completion),
                self.assertRaises(ModelBridgeError),
            ):
                run_model(
                    self.config,
                    "prompt",
                    self.runner_for(self.successful_stdout(completion)),
                )

    def test_rejects_nonzero_cli_exit(self) -> None:
        with self.assertRaisesRegex(ModelBridgeError, "exit code 17"):
            run_model(
                self.config, "prompt", self.runner_for(returncode=17, stderr="failure")
            )

    def test_rejects_timeout(self) -> None:
        runner = FakeRunner(
            error=subprocess.TimeoutExpired(
                build_codex_argv(self.config), self.config.timeout_seconds
            )
        )

        with self.assertRaisesRegex(ModelBridgeError, "timed out"):
            run_model(self.config, "prompt", runner)

    def test_rejects_malformed_jsonl_and_missing_usage(self) -> None:
        missing_usage = "\n".join(self.successful_stdout().splitlines()[:2]) + "\n"
        for stdout in ("{not-json}\n", missing_usage):
            with self.subTest(stdout=stdout), self.assertRaises(ModelBridgeError):
                run_model(self.config, "prompt", self.runner_for(stdout))

    def test_rejects_openai_keys_and_bearer_tokens(self) -> None:
        secrets = ("sk-proj-abcdefghijklmnopqrstuvwxyz012345", "Bearer abc.def.ghi")
        for secret in secrets:
            completion = self.completion()
            completion["explanation"] = f"accidental disclosure {secret}"
            with (
                self.subTest(secret=secret),
                self.assertRaisesRegex(ModelBridgeError, "secret"),
            ):
                run_model(
                    self.config,
                    "prompt",
                    self.runner_for(self.successful_stdout(completion)),
                )

    def test_context_manifest_hash_is_stable_and_content_sensitive(self) -> None:
        nested = self.context / "facts"
        nested.mkdir()
        (nested / "protocol.txt").write_bytes(b"frame=big-endian\n")
        manifest = [
            {
                "path": "facts/protocol.txt",
                "sha256": (
                    "0422c3cff900f2ee82b0fabe138b6c2a77e18d8e24d553021409b2c3cb4efd65"
                ),
                "size_bytes": 17,
            },
            {
                "path": "task.txt",
                "sha256": (
                    "90d33ef7847a501ae16e91825bb8d91d271067283fea854fb53187bcb0a707b7"
                ),
                "size_bytes": 13,
            },
        ]
        expected = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        self.assertEqual(context_manifest_sha256(self.context), expected)
        (self.context / "task.txt").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual(context_manifest_sha256(self.context), expected)

    def test_rejects_context_symlink(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("host data", encoding="utf-8")
        (self.context / "escape").symlink_to(outside)

        with self.assertRaisesRegex(ModelBridgeError, "symlink"):
            context_manifest_sha256(self.context)

        clean_context = Path(self.temporary.name) / "clean-context"
        clean_context.mkdir()
        (clean_context / "safe.txt").write_text("safe", encoding="utf-8")
        linked_root = Path(self.temporary.name) / "linked-context"
        linked_root.symlink_to(clean_context, target_is_directory=True)
        with self.assertRaisesRegex(ModelBridgeError, "symlink"):
            context_manifest_sha256(linked_root)

    def test_decimal_cost_formula_and_evidence_hashes(self) -> None:
        evidence = run_model(self.config, "prompt", self.runner_for())

        self.assertEqual(evidence.uncached_input_tokens, 100)
        self.assertEqual(evidence.cached_input_tokens, 20)
        self.assertEqual(evidence.output_tokens, 30)
        self.assertEqual(evidence.estimated_cost_usd, Decimal("0.000342"))
        self.assertEqual(evidence.prompt_sha256, hashlib.sha256(b"prompt").hexdigest())
        exact_message = json.dumps(self.completion()).encode("utf-8")
        self.assertEqual(
            evidence.output_sha256, hashlib.sha256(exact_message).hexdigest()
        )
        self.assertEqual(evidence.provider_request_count, 1)
        self.assertEqual(evidence.cli_version, "codex-cli 0.153.4")

    def test_rejects_unpinned_cli_version_evidence(self) -> None:
        stdout = self.successful_stdout().replace(
            "codex-cli 0.153.4", "codex-cli 999.0.0"
        )

        with self.assertRaisesRegex(ModelBridgeError, "CLI version"):
            run_model(self.config, "prompt", self.runner_for(stdout))

    def test_rejects_wrong_cost_limit_model_and_excess_cost(self) -> None:
        bad_limit = ModelRunConfig(
            **{**self.config.__dict__, "cost_limit_usd": Decimal("6.00")}
        )
        runner = self.runner_for()
        with self.assertRaisesRegex(ModelBridgeError, "exactly USD 5.00"):
            run_model(bad_limit, "prompt", runner)
        self.assertEqual(runner.calls, [])

        card = json.loads(self.rate_card.read_text(encoding="utf-8"))
        card["model"] = "gpt-6-astra"
        self.rate_card.write_text(json.dumps(card), encoding="utf-8")
        with self.assertRaisesRegex(ModelBridgeError, "model"):
            run_model(self.config, "prompt", self.runner_for())

        card["model"] = "gpt-5.6-sol"
        self.rate_card.write_text(json.dumps(card), encoding="utf-8")
        costly = self.runner_for(
            self.successful_stdout(
                input_tokens=6_000_000, cached_input_tokens=0, output_tokens=0
            )
        )
        with self.assertRaisesRegex(ModelBridgeError, "exceeds"):
            run_model(self.config, "prompt", costly)

    def test_context_symlink_fails_before_cli_invocation(self) -> None:
        clean_context = Path(self.temporary.name) / "preflight-context"
        clean_context.mkdir()
        linked_context = Path(self.temporary.name) / "linked-preflight-context"
        linked_context.symlink_to(clean_context, target_is_directory=True)
        config = ModelRunConfig(
            **{**self.config.__dict__, "context_dir": linked_context}
        )
        runner = self.runner_for()

        with self.assertRaisesRegex(ModelBridgeError, "symlink"):
            run_model(config, "prompt", runner)
        self.assertEqual(runner.calls, [])

    def test_rejects_missing_negative_or_inconsistent_usage(self) -> None:
        for values in ((-1, 0, 0), (10, -1, 0), (10, 0, -1), (10, 11, 0)):
            with self.subTest(values=values), self.assertRaises(ModelBridgeError):
                run_model(
                    self.config,
                    "prompt",
                    self.runner_for(
                        self.successful_stdout(
                            input_tokens=values[0],
                            cached_input_tokens=values[1],
                            output_tokens=values[2],
                        )
                    ),
                )

    def test_rejects_absent_provider_requests_and_records_multiple_requests(
        self,
    ) -> None:
        with self.assertRaisesRegex(ModelBridgeError, "provider"):
            run_model(
                self.config,
                "prompt",
                self.runner_for(self.successful_stdout(provider_request_count=0)),
            )

        evidence = run_model(
            self.config,
            "prompt",
            self.runner_for(self.successful_stdout(provider_request_count=2)),
        )
        self.assertEqual(evidence.provider_request_count, 2)


class ProviderProxyPolicyTests(unittest.TestCase):
    def test_allows_only_exact_provider_connect_destination(self) -> None:
        self.assertEqual(
            parse_allowed_connect_target("api.openai.com:443"), ("api.openai.com", 443)
        )
        self.assertEqual(
            parse_allowed_connect_target("API.OPENAI.COM:443"), ("api.openai.com", 443)
        )

    def test_denies_raw_ips_userinfo_ports_and_other_destinations(self) -> None:
        denied = (
            "104.18.7.192:443",
            "[2606:4700::6812:7c0]:443",
            "user@api.openai.com:443",
            "api.openai.com:80",
            "api.openai.com:8443",
            "api.openai.com:0443",
            "api.openai.com.:443",
            "example.com:443",
            "api.openai.com",
            "api.openai.com:443/path",
        )
        for authority in denied:
            with self.subTest(authority=authority), self.assertRaises(ProxyPolicyError):
                parse_allowed_connect_target(authority)

    def test_live_proxy_denies_non_connect_and_other_connect_without_upstream_io(
        self,
    ) -> None:
        audit = ProxyAudit()
        server = ProviderProxyServer(("127.0.0.1", 0), audit)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request("GET", "http://example.com/")
        self.assertEqual(connection.getresponse().status, 405)
        connection.close()

        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.set_tunnel("example.com", 443)
        with self.assertRaises(OSError):
            connection.connect()
        connection.close()
        self.assertEqual(audit.snapshot()["request_count"], 0)

    def test_denies_redirects_outside_provider(self) -> None:
        validate_redirect_target("https://api.openai.com/v1/responses")
        for location in (
            "http://api.openai.com/v1/responses",
            "https://api.openai.com:8443/v1/responses",
            "https://example.com/v1/responses",
            "//104.18.7.192/v1/responses",
        ):
            with self.subTest(location=location), self.assertRaises(ProxyPolicyError):
                validate_redirect_target(location)

    def test_proxy_audit_records_counts_and_bytes(self) -> None:
        audit = ProxyAudit()
        audit.record_request()
        audit.record_bytes(client_to_provider=11, provider_to_client=23)

        self.assertEqual(
            audit.snapshot(),
            {
                "request_count": 1,
                "client_to_provider_bytes": 11,
                "provider_to_client_bytes": 23,
            },
        )


if __name__ == "__main__":
    unittest.main()
