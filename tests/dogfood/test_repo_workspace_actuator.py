from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.dogfood.repo_workspace_actuator import (
    ACTUATOR_ID,
    COMMANDS,
    MAX_CONTENT_BYTES,
    ActuatorConfig,
    ActuatorError,
    RepoWorkspaceActuator,
    canonical_json,
    sha256_digest,
)


class RecordingRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0], returncode=self.returncode, stdout="stdout", stderr="stderr"
        )


class RecordingClient:
    def __init__(self, response, events: list[str], name: str) -> None:
        self.response = response
        self.events = events
        self.name = name
        self.calls: list[tuple[str, dict[str, object]]] = []

    def request(self, op: str, payload) -> dict[str, object]:
        self.events.append(self.name)
        self.calls.append((op, dict(payload)))
        return self.response


class RepoWorkspaceActuatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.target = root / "target"
        self.run_dir = root / "run"
        (self.target / "src/castor").mkdir(parents=True)
        (self.target / "tests").mkdir()
        self.run_dir.mkdir()
        self.secret = b"test-receipt-secret-that-is-at-least-32-bytes"
        self.config = ActuatorConfig(
            target_workspace=self.target,
            run_dir=self.run_dir,
            state_db=self.run_dir / "actuator.sqlite",
            actuator_socket=self.run_dir / "actuator.sock",
            evidence_socket=self.run_dir / "evidence.sock",
            actuator_secret=self.secret,
            issuer="dogfood-evidence-service",
            command_timeout_seconds=90,
        )

    def actuator(self, runner=None, crash_hook=None, **kwargs) -> RepoWorkspaceActuator:
        return RepoWorkspaceActuator(
            self.config,
            process_runner=runner or RecordingRunner(),
            crash_hook=crash_hook,
            **kwargs,
        )

    def envelope(
        self,
        payload: dict[str, object],
        *,
        attempt_id: int = 11,
        actuator_id: str = ACTUATOR_ID,
        target_scope: str | None = None,
    ) -> dict[str, object]:
        payload_bytes = canonical_json(payload)
        if target_scope is None:
            if payload["kind"] == "write_file":
                target_scope = f"repo:castor:file/{payload['path']}"
            else:
                target_scope = f"repo:castor:cmd/{payload['command_id']}"
        return {
            "delivery_outcome": "Delivered",
            "attempt_id": attempt_id,
            "action_id": f"action-{attempt_id}",
            "dispatch_identity": f"operation-{attempt_id}",
            "target_scope": target_scope,
            "payload_region_ref": f"region://dogfood/action-{attempt_id}",
            "payload_digest": sha256_digest(payload_bytes),
            "actuator_id": actuator_id,
            "payload": list(payload_bytes),
        }

    def test_file_policy_allows_exactly_two_paths(self) -> None:
        actuator = self.actuator()
        for index, path in enumerate(
            ("src/castor/ipc_client.py", "tests/test_ipc_client.py"), start=1
        ):
            envelope = self.envelope(
                {"kind": "write_file", "path": path, "content_utf8": f"file {index}\n"},
                attempt_id=index,
            )
            result = actuator.process_envelope(envelope)
            self.assertEqual(
                (self.target / path).read_text(encoding="utf-8"), f"file {index}\n"
            )
            self.assertEqual(result["receipt"]["actuator_state"], "Committed")

        for path in ("pyproject.toml", "src/castor/other.py"):
            with self.subTest(path=path), self.assertRaises(ActuatorError):
                actuator.process_envelope(
                    self.envelope(
                        {"kind": "write_file", "path": path, "content_utf8": "hostile"},
                        attempt_id=20 + len(path),
                    )
                )

    def test_rejects_traversal_absolute_escape_and_symlinks(self) -> None:
        outside = self.target.parent / "outside.py"
        allowed = self.target / "src/castor/ipc_client.py"
        allowed.symlink_to(outside)
        cases = (
            ("../outside.py", "repo:castor:file/../outside.py"),
            (str(outside), f"repo:castor:file/{outside}"),
            ("src/castor/ipc_client.py", "repo:castor:file/src/castor/ipc_client.py"),
        )
        for index, (path, scope) in enumerate(cases, start=1):
            with self.subTest(path=path), self.assertRaises(ActuatorError):
                self.actuator().process_envelope(
                    self.envelope(
                        {"kind": "write_file", "path": path, "content_utf8": "escape"},
                        attempt_id=30 + index,
                        target_scope=scope,
                    )
                )
        self.assertFalse(outside.exists())

    def test_rejects_invalid_utf8_and_content_over_65536_bytes(self) -> None:
        invalid = self.envelope(
            {
                "kind": "write_file",
                "path": "tests/test_ipc_client.py",
                "content_utf8": "valid",
            }
        )
        invalid["payload"] = [255]
        invalid["payload_digest"] = sha256_digest(b"\xff")
        with self.assertRaisesRegex(ActuatorError, "UTF-8"):
            self.actuator().process_envelope(invalid)

        too_large = {
            "kind": "write_file",
            "path": "tests/test_ipc_client.py",
            "content_utf8": "é" * ((MAX_CONTENT_BYTES // 2) + 1),
        }
        with self.assertRaisesRegex(ActuatorError, "65,536"):
            self.actuator().process_envelope(self.envelope(too_large, attempt_id=12))

    def test_rejects_payload_digest_mismatch_and_wrong_actuator(self) -> None:
        payload = {
            "kind": "write_file",
            "path": "tests/test_ipc_client.py",
            "content_utf8": "content\n",
        }
        mismatched = self.envelope(payload)
        mismatched["payload_digest"] = sha256_digest(b"different")
        wrong_actuator = self.envelope(payload, attempt_id=12, actuator_id="hostile")
        for envelope in (mismatched, wrong_actuator):
            with self.assertRaises(ActuatorError):
                self.actuator().process_envelope(envelope)

    def test_file_write_is_same_directory_atomic_and_digest_verified(self) -> None:
        path = self.target / "src/castor/ipc_client.py"
        path.write_text("old\n", encoding="utf-8")
        envelope = self.envelope(
            {
                "kind": "write_file",
                "path": "src/castor/ipc_client.py",
                "content_utf8": "new\n",
            }
        )
        real_replace = os.replace
        replacements: list[tuple[Path, Path]] = []

        def recording_replace(source, destination):
            replacements.append((Path(source), Path(destination)))
            return real_replace(source, destination)

        with mock.patch(
            "tests.dogfood.repo_workspace_actuator.os.replace",
            side_effect=recording_replace,
        ):
            self.actuator().process_envelope(envelope)

        self.assertEqual(path.read_bytes(), b"new\n")
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0][0].parent.resolve(), path.parent.resolve())
        self.assertEqual(replacements[0][1].resolve(), path.resolve())
        self.assertEqual(list(path.parent.glob(".castor-*.tmp")), [])

    def test_target_already_matching_does_not_replace(self) -> None:
        path = self.target / "tests/test_ipc_client.py"
        path.write_text("same\n", encoding="utf-8")
        envelope = self.envelope(
            {
                "kind": "write_file",
                "path": "tests/test_ipc_client.py",
                "content_utf8": "same\n",
            }
        )
        with mock.patch("tests.dogfood.repo_workspace_actuator.os.replace") as replace:
            result = self.actuator().process_envelope(envelope)
        replace.assert_not_called()
        self.assertEqual(result["physical_observation"], "already_matching")

    def test_duplicate_delivery_reuses_terminal_attempt_without_reapplying(
        self,
    ) -> None:
        envelope = self.envelope(
            {
                "kind": "write_file",
                "path": "src/castor/ipc_client.py",
                "content_utf8": "durable\n",
            }
        )
        first = self.actuator().process_envelope(envelope)
        duplicate = dict(envelope)
        duplicate["delivery_outcome"] = "DuplicateDelivery"

        with mock.patch("tests.dogfood.repo_workspace_actuator.os.replace") as replace:
            second = self.actuator().process_envelope(duplicate)

        replace.assert_not_called()
        self.assertEqual(second["receipt_bytes"], first["receipt_bytes"])

    def test_command_policy_uses_exact_argv_no_shell_and_redirected_state(self) -> None:
        expected = {
            "test_unittest": [
                "uv",
                "run",
                "--frozen",
                "--offline",
                "python",
                "-B",
                "-m",
                "unittest",
                "tests.test_ipc_client",
                "-v",
            ],
            "test_pytest": [
                "uv",
                "run",
                "--frozen",
                "--offline",
                "python",
                "-B",
                "-m",
                "pytest",
                "tests/test_ipc_client.py",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            "lint_check": [
                "uv",
                "run",
                "--frozen",
                "--offline",
                "ruff",
                "check",
                "--no-cache",
            ],
            "format_check": [
                "uv",
                "run",
                "--frozen",
                "--offline",
                "ruff",
                "format",
                "--check",
                "--no-cache",
            ],
            "baseline_rust": [
                "cargo",
                "test",
                "--manifest-path",
                "kernel/Cargo.toml",
                "--all-targets",
            ],
            "baseline_castord_python": [
                "uv",
                "run",
                "--frozen",
                "--offline",
                "python",
                "-B",
                "-m",
                "unittest",
                "tests/test_cognitive_recovery_castord.py",
                "tests/test_evidence_boundary_castord.py",
            ],
        }
        self.assertEqual(COMMANDS, expected)
        runner = RecordingRunner()
        results = []
        ambient = {
            "PATH": "/trusted/bin",
            "CASTOR_ACTUATOR_SECRET_HEX": "must-not-leak",
            "AWS_SECRET_ACCESS_KEY": "must-not-leak",
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            actuator = self.actuator(runner)
            for index, command_id in enumerate(expected, start=1):
                results.append(
                    actuator.process_envelope(
                        self.envelope(
                            {"kind": "run_command", "command_id": command_id},
                            attempt_id=100 + index,
                        )
                    )
                )

        self.assertEqual(
            [list(call[0][0]) for call in runner.calls], list(expected.values())
        )
        for _, kwargs in runner.calls:
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["cwd"], self.target.resolve())
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertEqual(kwargs["timeout"], 90)
            environment = kwargs["env"]
            self.assertEqual(environment["PATH"], "/trusted/bin")
            self.assertNotIn("CASTOR_ACTUATOR_SECRET_HEX", environment)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
            resolved_run = self.run_dir.resolve()
            self.assertEqual(
                environment["CARGO_TARGET_DIR"], str(resolved_run / "cargo-target")
            )
            self.assertEqual(
                environment["UV_CACHE_DIR"], str(resolved_run / "uv-cache")
            )
            self.assertEqual(
                environment["UV_PROJECT_ENVIRONMENT"], str(resolved_run / "venv")
            )
            self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(
                environment["PYTHONPYCACHEPREFIX"], str(resolved_run / "pycache")
            )
            self.assertEqual(
                environment["RUFF_CACHE_DIR"], str(resolved_run / "ruff-cache")
            )
            self.assertEqual(
                environment["CASTORD_BINARY"],
                str(resolved_run / "cargo-target/debug/castord"),
            )
        for command_id, result in zip(expected, results, strict=True):
            observation = json.loads(result["physical_observation"])
            self.assertEqual(observation["argv"], expected[command_id])
            self.assertEqual(observation["cwd"], str(self.target.resolve()))

    def test_rejects_unknown_command_and_caller_supplied_execution_fields(self) -> None:
        payloads = (
            {"kind": "run_command", "command_id": "rm_everything"},
            {"kind": "run_command", "command_id": "lint_check", "shell": "rm -rf ."},
            {"kind": "run_command", "command_id": "lint_check", "argv": ["--fix"]},
        )
        for index, payload in enumerate(payloads, start=1):
            with self.subTest(payload=payload), self.assertRaises(ActuatorError):
                self.actuator().process_envelope(
                    self.envelope(payload, attempt_id=200 + index)
                )

    def test_sqlite_reconciliation_recovers_replace_before_receipt(self) -> None:
        envelope = self.envelope(
            {
                "kind": "write_file",
                "path": "src/castor/ipc_client.py",
                "content_utf8": "recovered\n",
            }
        )

        def crash(phase: str) -> None:
            if phase == "after_file_replace":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.actuator(crash_hook=crash).process_envelope(envelope)
        with mock.patch("tests.dogfood.repo_workspace_actuator.os.replace") as replace:
            result = self.actuator().process_envelope(envelope)
        replace.assert_not_called()
        self.assertEqual(result["physical_observation"], "reconciled_matching")

        with sqlite3.connect(self.config.state_db) as db:
            row = db.execute(
                "SELECT envelope_digest, desired_content_digest, phase, "
                "physical_observation, receipt_bytes, terminal_state "
                "FROM attempts WHERE attempt_id = 11"
            ).fetchone()
        immutable_envelope = dict(envelope)
        del immutable_envelope["delivery_outcome"]
        self.assertEqual(row[0], sha256_digest(canonical_json(immutable_envelope)))
        self.assertEqual(row[1], sha256_digest(b"recovered\n"))
        self.assertEqual(row[2], "terminal")
        self.assertEqual(row[3], "reconciled_matching")
        self.assertIsInstance(row[4], bytes)
        self.assertEqual(row[5], "Committed")

    def test_crash_after_effect_claim_resumes_the_same_file_attempt(self) -> None:
        envelope = self.envelope(
            {
                "kind": "write_file",
                "path": "src/castor/ipc_client.py",
                "content_utf8": "claimed\n",
            }
        )

        def crash(phase: str) -> None:
            if phase == "after_effect_claim":
                raise RuntimeError("simulated claimant crash")

        with self.assertRaisesRegex(RuntimeError, "claimant crash"):
            self.actuator(crash_hook=crash).process_envelope(envelope)
        result = self.actuator().process_envelope(envelope)

        self.assertEqual(
            (self.target / "src/castor/ipc_client.py").read_bytes(), b"claimed\n"
        )
        self.assertEqual(result["physical_observation"], "resumed_replaced")

    def test_state_database_must_be_contained_by_disjoint_run_directory(self) -> None:
        outside = self.target.parent / "outside.sqlite"
        invalid = ActuatorConfig(
            **{
                **self.config.__dict__,
                "state_db": outside,
            }
        )
        with self.assertRaisesRegex(ActuatorError, "state database"):
            RepoWorkspaceActuator(invalid)

    def test_receipt_has_exact_signed_body_and_terminal_settlement_fields(self) -> None:
        envelope = self.envelope(
            {
                "kind": "write_file",
                "path": "tests/test_ipc_client.py",
                "content_utf8": "receipt\n",
            }
        )
        result = self.actuator().process_envelope(envelope)
        receipt = result["receipt"]
        signature = receipt.pop("signature")
        self.assertEqual(
            set(receipt),
            {
                "attempt_id",
                "stable_operation_id",
                "request_digest",
                "issuer",
                "adapter_id",
                "settlement_schema_version",
                "resolution",
                "actuator_state",
            },
        )
        self.assertEqual(
            signature,
            hmac.new(self.secret, canonical_json(receipt), hashlib.sha256).hexdigest(),
        )
        settlement = result["settlement"]
        self.assertEqual(settlement["dispatch_identity"], "operation-11")
        self.assertEqual(settlement["proof_class"], "ProviderConfirmation")
        self.assertEqual(settlement["resolution"], "Confirmed")
        self.assertEqual(settlement["actuator_state"], "Committed")
        self.assertEqual(
            settlement["evidence_digest"], sha256_digest(result["receipt_bytes"])
        )

    def test_acquire_publishes_receipt_before_exact_successful_settlement(self) -> None:
        envelope = self.envelope(
            {
                "kind": "write_file",
                "path": "tests/test_ipc_client.py",
                "content_utf8": "settle\n",
            }
        )
        events: list[str] = []
        actuator_client = RecordingClient(
            {"status": "Ok", "outcome": envelope}, events, "acquire"
        )
        evidence_client = RecordingClient(
            {
                "status": "Ok",
                "outcome": {"type": "Settled", "resolution": "Confirmed"},
            },
            events,
            "settle",
        )

        def publish(region_id: str, region_digest: str, receipt_bytes: bytes) -> None:
            events.append("publish")
            self.assertEqual(region_digest, sha256_digest(receipt_bytes))
            self.assertTrue(region_id.startswith("region://dogfood/receipt/"))

        result = self.actuator(
            actuator_client=actuator_client,
            evidence_client=evidence_client,
        ).acquire_and_settle(11, "operation-11", publish)

        self.assertEqual(events, ["acquire", "publish", "settle"])
        self.assertEqual(result["receipt"]["actuator_state"], "Committed")
        rejected = RecordingClient(
            {"status": "Ok", "outcome": {"type": "RejectedCurrentState"}},
            [],
            "settle",
        )
        with self.assertRaisesRegex(ActuatorError, "settlement rejected"):
            self.actuator(
                actuator_client=actuator_client,
                evidence_client=rejected,
            ).acquire_and_settle(11, "operation-11", lambda *_args: None)


if __name__ == "__main__":
    unittest.main()
