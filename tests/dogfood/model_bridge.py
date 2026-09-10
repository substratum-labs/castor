from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import jsonschema

PINNED_CLI_VERSION = "codex-cli 0.153.4"
REQUIRED_COST_LIMIT = Decimal("5.00")
MAX_CONTENT_BYTES = 65_536
RATE_FIELDS = (
    "uncached_input_usd_per_million",
    "cached_input_usd_per_million",
    "output_usd_per_million",
)
SECRET_PATTERNS = (
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]*", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE),
)


class ModelBridgeError(RuntimeError):
    """A fail-closed model bridge validation error."""


@dataclass(frozen=True)
class ModelRunConfig:
    model: str
    reasoning_effort: str
    context_dir: Path
    output_schema: Path
    rate_card_path: Path
    cost_limit_usd: Decimal
    provider_proxy_url: str
    timeout_seconds: int


@dataclass(frozen=True)
class ModelRunEvidence:
    completion: dict[str, object]
    prompt_sha256: str
    context_manifest_sha256: str
    output_sha256: str
    cli_version: str
    exit_code: int
    provider_request_count: int
    uncached_input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    rate_card_sha256: str
    estimated_cost_usd: Decimal


class ProcessRunner(Protocol):
    def run(
        self,
        argv: list[str],
        stdin_text: str,
        timeout_seconds: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]: ...


PreflightRunner = Callable[..., subprocess.CompletedProcess[str]]


def build_codex_argv(config: ModelRunConfig) -> list[str]:
    return [
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--model",
        config.model,
        "--config",
        f'model_reasoning_effort="{config.reasoning_effort}"',
        "-C",
        str(config.context_dir),
        "--skip-git-repo-check",
        "--output-schema",
        str(config.output_schema),
        "--json",
        "-",
    ]


def build_model_env(config: ModelRunConfig) -> dict[str, str]:
    _validate_proxy_url(config.provider_proxy_url)
    env: dict[str, str] = {}
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        env["OPENAI_API_KEY"] = api_key
    env["HTTP_PROXY"] = config.provider_proxy_url
    env["HTTPS_PROXY"] = config.provider_proxy_url
    return env


def context_manifest_sha256(context_dir: Path) -> str:
    if context_dir.is_symlink():
        raise ModelBridgeError("context directory symlink is forbidden")
    root = context_dir.resolve(strict=True)
    if not root.is_dir():
        raise ModelBridgeError("context directory is not a directory")
    manifest: list[dict[str, object]] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        if path.is_symlink():
            raise ModelBridgeError(
                f"context symlink is forbidden: {path.relative_to(root)}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ModelBridgeError(
                f"context contains a non-regular file: {path.relative_to(root)}"
            )
        content = path.read_bytes()
        manifest.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def run_model(
    config: ModelRunConfig, prompt: str, runner: ProcessRunner
) -> ModelRunEvidence:
    _validate_config_before_run(config)
    argv = build_codex_argv(config)
    try:
        completed = runner.run(
            argv,
            stdin_text=prompt,
            timeout_seconds=config.timeout_seconds,
            env=build_model_env(config),
        )
    except subprocess.TimeoutExpired as error:
        raise ModelBridgeError(
            f"Codex CLI timed out after {config.timeout_seconds} seconds"
        ) from error
    return validate_model_run(config, completed, prompt=prompt)


def _validate_config_before_run(config: ModelRunConfig) -> None:
    if config.cost_limit_usd != REQUIRED_COST_LIMIT:
        raise ModelBridgeError("configured cost limit must be exactly USD 5.00")
    if config.timeout_seconds <= 0:
        raise ModelBridgeError("model timeout must be positive")
    context_manifest_sha256(config.context_dir)
    _load_output_validator(config.output_schema)
    _load_rate_card(config)


def validate_model_run(
    config: ModelRunConfig,
    completed: subprocess.CompletedProcess[str],
    prompt: str = "",
) -> ModelRunEvidence:
    if config.cost_limit_usd != REQUIRED_COST_LIMIT:
        raise ModelBridgeError("configured cost limit must be exactly USD 5.00")
    _reject_secrets(completed.stdout)
    _reject_secrets(completed.stderr)
    if completed.returncode != 0:
        raise ModelBridgeError(
            f"Codex CLI exited with exit code {completed.returncode}"
        )

    records = _parse_jsonl(completed.stdout)
    completion, completion_bytes = _extract_completion(records)
    _validate_completion(config.output_schema, completion)
    usage = _extract_usage(records)
    cli_version, provider_request_count = _extract_audit(records)
    rate_card, rate_card_bytes = _load_rate_card(config)

    input_tokens, cached_input_tokens, output_tokens = usage
    uncached_input_tokens = input_tokens - cached_input_tokens
    estimated_cost = (
        Decimal(uncached_input_tokens) * rate_card["uncached_input_usd_per_million"]
        + Decimal(cached_input_tokens) * rate_card["cached_input_usd_per_million"]
        + Decimal(output_tokens) * rate_card["output_usd_per_million"]
    ) / Decimal(1_000_000)
    if estimated_cost > config.cost_limit_usd:
        raise ModelBridgeError(
            f"estimated provider cost {estimated_cost} exceeds "
            f"USD {config.cost_limit_usd}"
        )

    return ModelRunEvidence(
        completion=completion,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        context_manifest_sha256=context_manifest_sha256(config.context_dir),
        output_sha256=hashlib.sha256(completion_bytes).hexdigest(),
        cli_version=cli_version,
        exit_code=completed.returncode,
        provider_request_count=provider_request_count,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        rate_card_sha256=hashlib.sha256(rate_card_bytes).hexdigest(),
        estimated_cost_usd=estimated_cost,
    )


def _validate_proxy_url(proxy_url: str) -> None:
    parsed = urlsplit(proxy_url)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ModelBridgeError(
            "provider proxy URL must be an HTTP URL without userinfo"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise ModelBridgeError("provider proxy URL has an invalid port") from error
    if port is None or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ModelBridgeError(
            "provider proxy URL must contain only an explicit host and port"
        )
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    raise ModelBridgeError(
        "provider proxy URL must use the audited proxy hostname, not a raw IP"
    )


def _reject_secrets(text: str) -> None:
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise ModelBridgeError("model output contains a forbidden secret pattern")


def _parse_jsonl(stdout: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ModelBridgeError(
                f"malformed Codex JSONL at line {line_number}"
            ) from error
        if not isinstance(record, dict):
            raise ModelBridgeError(f"Codex JSONL line {line_number} is not an object")
        records.append(record)
    if not records:
        raise ModelBridgeError("Codex CLI returned no JSONL records")
    return records


def _extract_completion(
    records: list[dict[str, object]],
) -> tuple[dict[str, object], bytes]:
    messages: list[tuple[dict[str, object], bytes]] = []
    for record in records:
        if record.get("type") != "item.completed":
            continue
        item = record.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            raise ModelBridgeError("structured agent message is missing text")
        try:
            completion = json.loads(text)
        except json.JSONDecodeError as error:
            raise ModelBridgeError(
                "final structured model message is malformed JSON"
            ) from error
        if not isinstance(completion, dict):
            raise ModelBridgeError("final structured model message must be an object")
        messages.append((completion, text.encode("utf-8")))
    if len(messages) != 1:
        raise ModelBridgeError(
            "Codex JSONL must contain exactly one structured agent message"
        )
    return messages[0]


def _extract_usage(records: list[dict[str, object]]) -> tuple[int, int, int]:
    usage_records = [
        record.get("usage")
        for record in records
        if record.get("type") == "turn.completed"
    ]
    if len(usage_records) != 1 or not isinstance(usage_records[0], dict):
        raise ModelBridgeError(
            "Codex JSONL must contain exactly one complete usage record"
        )
    usage = usage_records[0]
    values = tuple(
        usage.get(field)
        for field in ("input_tokens", "cached_input_tokens", "output_tokens")
    )
    if any(type(value) is not int or value < 0 for value in values):
        raise ModelBridgeError(
            "token usage categories must be present non-negative integers"
        )
    input_tokens, cached_input_tokens, output_tokens = values
    if cached_input_tokens > input_tokens:
        raise ModelBridgeError("cached input tokens cannot exceed total input tokens")
    return input_tokens, cached_input_tokens, output_tokens


def _extract_audit(records: list[dict[str, object]]) -> tuple[str, int]:
    audits = [
        record
        for record in records
        if record.get("type") in {"bridge.audit", "provider.audit"}
    ]
    thread_count = sum(record.get("type") == "thread.started" for record in records)
    if len(audits) > 1:
        raise ModelBridgeError("Codex JSONL contains multiple provider audit records")
    audit = audits[0] if audits else {}
    cli_version = audit.get("cli_version", PINNED_CLI_VERSION)
    provider_request_count = audit.get("provider_request_count", thread_count)
    if cli_version != PINNED_CLI_VERSION:
        raise ModelBridgeError(
            f"CLI version evidence must equal pinned {PINNED_CLI_VERSION}"
        )
    if type(provider_request_count) is not int or provider_request_count < 1:
        raise ModelBridgeError("provider audit must prove at least one request")
    return cli_version, provider_request_count


def _validate_completion(schema_path: Path, completion: dict[str, object]) -> None:
    try:
        _load_output_validator(schema_path).validate(completion)
    except (jsonschema.ValidationError,) as error:
        raise ModelBridgeError(
            "model completion failed its closed output schema"
        ) from error

    files = completion.get("files")
    assert isinstance(files, list)
    for item in files:
        assert isinstance(item, dict)
        content = item.get("content_utf8")
        assert isinstance(content, str)
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ModelBridgeError("file content exceeds 65,536 UTF-8 bytes")


def _load_output_validator(path: Path) -> jsonschema.protocols.Validator:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        return validator_class(schema)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as error:
        raise ModelBridgeError(
            "model output schema is unavailable or invalid"
        ) from error


def _load_rate_card(config: ModelRunConfig) -> tuple[dict[str, Decimal], bytes]:
    schema_path = Path(__file__).with_name("rate_card.schema.json")
    try:
        rate_card_bytes = config.rate_card_path.read_bytes()
        rate_card = json.loads(rate_card_bytes)
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        validator_class(schema, format_checker=jsonschema.FormatChecker()).validate(
            rate_card
        )
    except (
        OSError,
        json.JSONDecodeError,
        jsonschema.SchemaError,
        jsonschema.ValidationError,
    ) as error:
        raise ModelBridgeError("rate card failed its closed schema") from error
    if rate_card["model"] != config.model:
        raise ModelBridgeError("rate-card model does not match configured model")
    if (
        rate_card["currency"] != "USD"
        or rate_card["output_tokens_include_reasoning"] is not True
    ):
        raise ModelBridgeError(
            "rate card has unsupported currency or output-token accounting"
        )
    source = urlsplit(rate_card["source_url"])
    if source.scheme != "https" or source.hostname not in {
        "openai.com",
        "developers.openai.com",
    }:
        raise ModelBridgeError(
            "rate card source is not an approved official OpenAI URL"
        )
    prices: dict[str, Decimal] = {}
    try:
        for field in RATE_FIELDS:
            value = Decimal(rate_card[field])
            if not value.is_finite() or value < 0:
                raise ModelBridgeError(
                    "rate-card prices must be finite and non-negative"
                )
            prices[field] = value
    except (InvalidOperation, KeyError, TypeError) as error:
        raise ModelBridgeError("rate-card prices must be decimal strings") from error
    return prices, rate_card_bytes


def run_outer_sandbox_preflight(
    runner: PreflightRunner = subprocess.run,
) -> dict[str, bool]:
    dogfood_dir = Path(__file__).resolve().parent
    host_worktree = Path.cwd().resolve()
    with tempfile.TemporaryDirectory(prefix="castor-model-preflight-") as temporary:
        context_dir = Path(temporary) / "context"
        context_dir.mkdir()
        (context_dir / "marker.txt").write_text(
            "curated-model-context\n", encoding="utf-8"
        )
        probe_script = f"""
const fs = require("fs");
const net = require("net");
const result = {{
  curated_read:
    fs.readFileSync("/context/marker.txt", "utf8") ===
    "curated-model-context\\n",
  schema_read:
    JSON.parse(
      fs.readFileSync("/schema/model_output.schema.json", "utf8")
    ).type === "object",
  host_path_denied: !fs.existsSync({json.dumps(str(host_worktree))}),
  arbitrary_egress_denied: false,
}};
let finished = false;
function finish(denied) {{
  if (finished) return;
  finished = true;
  result.arbitrary_egress_denied = denied;
  process.stdout.write(JSON.stringify(result) + "\\n");
  process.exit(denied ? 0 : 9);
}}
const connection = net.createConnection({{host: "203.0.113.1", port: 443}});
connection.on("connect", () => finish(false));
connection.on("error", () => finish(true));
setTimeout(() => finish(false), 2000);
""".strip()
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10002:10002",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--mount",
            f"type=bind,src={context_dir},dst=/context,readonly",
            "--mount",
            (
                f"type=bind,src={dogfood_dir / 'model_output.schema.json'},"
                "dst=/schema/model_output.schema.json,readonly"
            ),
            "--entrypoint",
            "node",
            "castor-dogfood-model:t320d",
            "-e",
            probe_script,
        ]
        try:
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ModelBridgeError(
                "outer model sandbox preflight could not run"
            ) from error
    if completed.returncode != 0:
        raise ModelBridgeError(
            "outer model sandbox preflight failed with exit code "
            f"{completed.returncode}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ModelBridgeError(
            "outer model sandbox preflight returned malformed JSON"
        ) from error
    required = {
        "arbitrary_egress_denied": True,
        "curated_read": True,
        "host_path_denied": True,
        "schema_read": True,
    }
    if result != required:
        raise ModelBridgeError(
            "outer model sandbox preflight did not prove containment"
        )
    return required


def _preflight(deny_provider_call: bool) -> int:
    if not deny_provider_call:
        raise ModelBridgeError("preflight requires --deny-provider-call")
    dogfood_dir = Path(__file__).resolve().parent
    for name in ("model_output.schema.json", "rate_card.schema.json"):
        schema = json.loads((dogfood_dir / name).read_text(encoding="utf-8"))
        jsonschema.validators.validator_for(schema).check_schema(schema)
    rate_schema = json.loads(
        (dogfood_dir / "rate_card.schema.json").read_text(encoding="utf-8")
    )
    fixture = json.loads(
        (dogfood_dir / "fixtures" / "rate_card_valid.json").read_text(encoding="utf-8")
    )
    jsonschema.validators.validator_for(rate_schema)(
        rate_schema, format_checker=jsonschema.FormatChecker()
    ).validate(fixture)
    containment = run_outer_sandbox_preflight()
    print(
        json.dumps(
            {
                **containment,
                "preflight": "pass",
                "provider_call_denied": True,
                "schemas_valid": True,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sandboxed physical Codex model bridge"
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--deny-provider-call", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.preflight_only:
            return _preflight(args.deny_provider_call)
        raise ModelBridgeError(
            "physical provider execution is available only through run_model"
        )
    except ModelBridgeError as error:
        print(f"model bridge refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
