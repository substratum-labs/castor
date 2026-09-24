"""Closed trusted actuator for the two-file coding dogfood workspace."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import struct
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ACTUATOR_ID = "repo-workspace-actuator"
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_CONTENT_BYTES = 65_536
WRITABLE_PATHS = frozenset(("src/castor/ipc_client.py", "tests/test_ipc_client.py"))
COMMANDS = {
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


class ActuatorError(RuntimeError):
    """An envelope, policy, or reconciliation failure."""


@dataclass(frozen=True)
class ActuatorConfig:
    target_workspace: Path
    run_dir: Path
    state_db: Path
    actuator_socket: Path
    evidence_socket: Path
    actuator_secret: bytes
    issuer: str
    command_timeout_seconds: int = 600
    actuator_id: str = ACTUATOR_ID


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class SocketClient:
    def __init__(self, path: Path, timeout_seconds: float = 5.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds

    def request(self, op: str, payload: Mapping[str, object]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        raw = canonical_json(
            {"request_id": request_id, "op": op, "payload": dict(payload)}
        )
        if not raw or len(raw) > MAX_FRAME_BYTES:
            raise ActuatorError("AISA frame length must be between 1 and 16 MiB")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(self.timeout_seconds)
            stream.connect(str(self.path))
            stream.sendall(struct.pack(">I", len(raw)) + raw)
            header = self._recv_exact(stream, 4)
            length = struct.unpack(">I", header)[0]
            if length == 0 or length > MAX_FRAME_BYTES:
                raise ActuatorError("invalid AISA frame length")
            response_raw = self._recv_exact(stream, length)
        try:
            response = json.loads(response_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ActuatorError("invalid AISA response") from error
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise ActuatorError("AISA response request_id mismatch")
        return response

    @staticmethod
    def _recv_exact(stream: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            part = stream.recv(size - len(data))
            if not part:
                raise ActuatorError("AISA peer closed a partial frame")
            data.extend(part)
        return bytes(data)


class RequestClient(Protocol):
    def request(self, op: str, payload: Mapping[str, object]) -> dict[str, Any]: ...


ReceiptPublisher = Callable[[str, str, bytes], None]


class RepoWorkspaceActuator:
    def __init__(
        self,
        config: ActuatorConfig,
        *,
        process_runner: Callable[
            ..., subprocess.CompletedProcess[str]
        ] = subprocess.run,
        crash_hook: Callable[[str], None] | None = None,
        actuator_client: RequestClient | None = None,
        evidence_client: RequestClient | None = None,
    ) -> None:
        self.config = config
        self.process_runner = process_runner
        self.crash_hook = crash_hook or (lambda _phase: None)
        self.actuator_client = actuator_client or SocketClient(config.actuator_socket)
        self.evidence_client = evidence_client or SocketClient(config.evidence_socket)
        if config.actuator_id != ACTUATOR_ID:
            raise ActuatorError("wrong configured actuator ID")
        if len(config.actuator_secret) < 32:
            raise ActuatorError(
                "actuator receipt secret must contain at least 32 bytes"
            )
        if config.command_timeout_seconds <= 0:
            raise ActuatorError("command timeout must be positive")
        self.target = config.target_workspace.resolve(strict=True)
        self.run_dir = config.run_dir.resolve(strict=True)
        if not self.target.is_dir() or config.target_workspace.is_symlink():
            raise ActuatorError("target workspace must be a real directory")
        if not self.run_dir.is_dir() or config.run_dir.is_symlink():
            raise ActuatorError("run directory must be a real directory")
        if (
            self.run_dir == self.target
            or self.target in self.run_dir.parents
            or self.run_dir in self.target.parents
        ):
            raise ActuatorError("run directory must be outside the target workspace")
        state_parent = config.state_db.parent
        state_parent.mkdir(parents=True, exist_ok=True)
        if config.state_db.is_symlink() or not self._inside_run(state_parent.resolve()):
            raise ActuatorError("state database must be contained by the run directory")
        for socket_path in (config.actuator_socket, config.evidence_socket):
            if socket_path.is_symlink() or not self._inside_run(
                socket_path.parent.resolve(strict=True)
            ):
                raise ActuatorError("socket must be contained by the run directory")
        self._prepare_generated_paths()
        self._initialize_db()

    def _inside_run(self, path: Path) -> bool:
        return path == self.run_dir or self.run_dir in path.parents

    def _prepare_generated_paths(self) -> None:
        for relative in (
            "cargo-target",
            "uv-cache",
            "venv",
            "pycache",
            "ruff-cache",
            "home",
            "tmp",
        ):
            path = self.run_dir / relative
            if path.is_symlink():
                raise ActuatorError("generated-state symlink is forbidden")
            path.mkdir(exist_ok=True)
            if not self._inside_run(path.resolve(strict=True)):
                raise ActuatorError("generated-state path escapes the run directory")

    @property
    def fixed_env(self) -> dict[str, str]:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.run_dir / "home"),
            "TMPDIR": str(self.run_dir / "tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        paths = {
            "CARGO_TARGET_DIR": self.run_dir / "cargo-target",
            "UV_CACHE_DIR": self.run_dir / "uv-cache",
            "UV_PROJECT_ENVIRONMENT": self.run_dir / "venv",
            "PYTHONPYCACHEPREFIX": self.run_dir / "pycache",
            "RUFF_CACHE_DIR": self.run_dir / "ruff-cache",
            "CASTORD_BINARY": self.run_dir / "cargo-target/debug/castord",
        }
        environment.update({name: str(path) for name, path in paths.items()})
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.config.state_db)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize_db(self) -> None:
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id INTEGER PRIMARY KEY,
                    envelope_digest TEXT NOT NULL,
                    desired_content_digest TEXT,
                    phase TEXT NOT NULL,
                    physical_observation TEXT,
                    receipt_bytes BLOB,
                    terminal_state TEXT
                )
                """
            )

    def process_envelope(self, envelope: Mapping[str, object]) -> dict[str, Any]:
        normalized = self._validate_envelope(envelope)
        with self._attempt_lock(normalized["attempt_id"]):
            return self._process_envelope_locked(envelope, normalized)

    @contextmanager
    def _attempt_lock(self, attempt_id: int) -> Iterator[None]:
        lock_path = self.run_dir / f"attempt-{attempt_id}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _process_envelope_locked(
        self,
        envelope: Mapping[str, object],
        normalized: dict[str, Any],
    ) -> dict[str, Any]:
        immutable_envelope = dict(envelope)
        del immutable_envelope["delivery_outcome"]
        envelope_digest = sha256_digest(canonical_json(immutable_envelope))
        payload = normalized["payload_value"]
        attempt_id = normalized["attempt_id"]
        desired_digest: str | None = None
        if payload["kind"] == "write_file":
            desired_digest = sha256_digest(payload["content_utf8"].encode("utf-8"))

        existing = self._record_received(attempt_id, envelope_digest, desired_digest)
        if existing is not None and existing[0] == "terminal":
            receipt_bytes = existing[2]
            if not isinstance(receipt_bytes, bytes):
                raise ActuatorError("terminal Attempt is missing its receipt")
            return self._result(
                normalized,
                receipt_bytes,
                existing[1] or "already_terminal",
            )

        if payload["kind"] == "write_file":
            observation = self._apply_file(
                attempt_id,
                payload["path"],
                payload["content_utf8"],
                desired_digest,
                existing,
            )
        else:
            observation = self._run_command(attempt_id, payload["command_id"], existing)

        receipt = self._receipt(normalized, "Confirmed", "Committed")
        receipt_bytes = canonical_json(receipt)
        with self._connect() as database:
            database.execute(
                "UPDATE attempts SET phase='terminal', physical_observation=?, "
                "receipt_bytes=?, terminal_state='Committed' WHERE attempt_id=?",
                (observation, receipt_bytes, attempt_id),
            )
        return self._result(normalized, receipt_bytes, observation)

    def _validate_envelope(self, envelope: Mapping[str, object]) -> dict[str, Any]:
        required = {
            "delivery_outcome",
            "attempt_id",
            "action_id",
            "dispatch_identity",
            "target_scope",
            "payload_region_ref",
            "payload_digest",
            "actuator_id",
            "payload",
        }
        if set(envelope) != required:
            raise ActuatorError(
                "delivery envelope fields do not match the closed schema"
            )
        if envelope["delivery_outcome"] not in ("Delivered", "DuplicateDelivery"):
            raise ActuatorError("Attempt did not deliver executable payload")
        if envelope["actuator_id"] != self.config.actuator_id:
            raise ActuatorError("wrong actuator ID")
        attempt_id = envelope["attempt_id"]
        if (
            not isinstance(attempt_id, int)
            or isinstance(attempt_id, bool)
            or attempt_id <= 0
        ):
            raise ActuatorError("attempt_id must be a positive integer")
        for field in (
            "action_id",
            "dispatch_identity",
            "target_scope",
            "payload_region_ref",
            "payload_digest",
        ):
            if not isinstance(envelope[field], str) or not envelope[field]:
                raise ActuatorError(f"{field} must be a nonempty string")
        payload_values = envelope["payload"]
        if not isinstance(payload_values, list) or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 255
            for value in payload_values
        ):
            raise ActuatorError("payload must be a byte array")
        payload_bytes = bytes(payload_values)
        if sha256_digest(payload_bytes) != envelope["payload_digest"]:
            raise ActuatorError("payload digest mismatch")
        try:
            payload_value = json.loads(payload_bytes.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ActuatorError("payload is not valid UTF-8") from error
        except json.JSONDecodeError as error:
            raise ActuatorError("payload is not valid JSON") from error
        if not isinstance(payload_value, dict):
            raise ActuatorError("payload must be an object")
        kind = payload_value.get("kind")
        if kind == "write_file":
            self._validate_file_payload(payload_value, str(envelope["target_scope"]))
        elif kind == "run_command":
            self._validate_command_payload(payload_value, str(envelope["target_scope"]))
        else:
            raise ActuatorError("unlisted actuator payload kind")
        return {**dict(envelope), "payload_value": payload_value}

    def _validate_file_payload(self, payload: dict[str, object], scope: str) -> None:
        if set(payload) != {"kind", "path", "content_utf8"}:
            raise ActuatorError("file payload fields do not match the closed schema")
        path_value = payload["path"]
        content = payload["content_utf8"]
        if not isinstance(path_value, str) or not isinstance(content, str):
            raise ActuatorError("file path and content_utf8 must be strings")
        path = Path(path_value)
        if path.is_absolute() or ".." in path.parts or path_value not in WRITABLE_PATHS:
            raise ActuatorError("file path is outside the writable policy")
        if scope != f"repo:castor:file/{path_value}":
            raise ActuatorError("file target scope does not match payload path")
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ActuatorError("content_utf8 is not valid UTF-8") from error
        if len(content_bytes) > MAX_CONTENT_BYTES:
            raise ActuatorError("file content exceeds 65,536 UTF-8 bytes")
        self._resolved_target(path_value)

    def _validate_command_payload(self, payload: dict[str, object], scope: str) -> None:
        if set(payload) != {"kind", "command_id"}:
            raise ActuatorError("command payload fields do not match the closed schema")
        command_id = payload["command_id"]
        if not isinstance(command_id, str) or command_id not in COMMANDS:
            raise ActuatorError("unlisted command ID")
        if scope != f"repo:castor:cmd/{command_id}":
            raise ActuatorError("command target scope does not match command ID")

    def _resolved_target(self, relative: str) -> Path:
        candidate = self.target / relative
        current = self.target
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ActuatorError("symlink targets are forbidden")
        parent = candidate.parent.resolve(strict=True)
        if parent != self.target and self.target not in parent.parents:
            raise ActuatorError("resolved path escapes target workspace")
        if candidate.exists() and not candidate.is_file():
            raise ActuatorError("target is not a regular file")
        return candidate

    def _record_received(
        self, attempt_id: int, envelope_digest: str, desired_digest: str | None
    ) -> tuple[str, str | None, bytes | None] | None:
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT envelope_digest, desired_content_digest, phase, "
                "physical_observation, receipt_bytes FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                database.execute(
                    "INSERT INTO attempts VALUES "
                    "(?, ?, ?, 'received', NULL, NULL, NULL)",
                    (attempt_id, envelope_digest, desired_digest),
                )
                return None
            if row[0] != envelope_digest or row[1] != desired_digest:
                raise ActuatorError("attempt_id was rebound to a different envelope")
            return row[2], row[3], row[4]

    def _current_digest(self, path: Path) -> str | None:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ActuatorError("target became a symlink or non-regular file")
        return sha256_digest(path.read_bytes())

    def _apply_file(
        self,
        attempt_id: int,
        relative: str,
        content: str,
        desired_digest: str | None,
        existing: tuple[str, str | None, bytes | None] | None,
    ) -> str:
        assert desired_digest is not None
        path = self._resolved_target(relative)
        current_digest = self._current_digest(path)
        if current_digest == desired_digest:
            return "reconciled_matching" if existing is not None else "already_matching"
        if existing is not None and existing[0] == "command_running":
            raise ActuatorError("in-flight Attempt cannot be safely replayed")
        resumed = existing is not None and existing[0] == "applying"
        if not resumed:
            self._claim_effect(attempt_id, "applying", current_digest or "missing")
            self.crash_hook("after_effect_claim")
        self._atomic_replace(path, content.encode("utf-8"))
        self.crash_hook("after_file_replace")
        if self._current_digest(path) != desired_digest:
            raise ActuatorError("post-write SHA-256 digest mismatch")
        return "resumed_replaced" if resumed else "replaced"

    def _atomic_replace(self, path: Path, content: bytes) -> None:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".castor-", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _run_command(
        self,
        attempt_id: int,
        command_id: str,
        existing: tuple[str, str | None, bytes | None] | None,
    ) -> str:
        if existing is not None and existing[0] == "command_running":
            with self._connect() as database:
                database.execute(
                    "UPDATE attempts SET physical_observation="
                    "'ambiguous_after_command_start' WHERE attempt_id=?",
                    (attempt_id,),
                )
            raise ActuatorError(
                "in-flight command Attempt is ambiguous and will not rerun"
            )
        self._claim_effect(attempt_id, "command_running", "not_started")
        self.crash_hook("after_effect_claim")
        completed = self.process_runner(
            list(COMMANDS[command_id]),
            cwd=self.target,
            env=self.fixed_env,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.config.command_timeout_seconds,
        )
        return canonical_json(
            {
                "command_id": command_id,
                "argv": COMMANDS[command_id],
                "cwd": str(self.target),
                "exit_code": completed.returncode,
                "stdout_sha256": sha256_digest(completed.stdout.encode("utf-8")),
                "stderr_sha256": sha256_digest(completed.stderr.encode("utf-8")),
            }
        ).decode("utf-8")

    def _claim_effect(
        self, attempt_id: int, claimed_phase: str, observation: str
    ) -> None:
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            cursor = database.execute(
                "UPDATE attempts SET phase=?, physical_observation=? "
                "WHERE attempt_id=? AND phase='received'",
                (claimed_phase, observation, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ActuatorError("in-flight Attempt is already claimed")

    def _receipt(
        self, envelope: Mapping[str, object], resolution: str, actuator_state: str
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "attempt_id": envelope["attempt_id"],
            "stable_operation_id": envelope["dispatch_identity"],
            "request_digest": envelope["target_scope"],
            "issuer": self.config.issuer,
            "adapter_id": self.config.actuator_id,
            "settlement_schema_version": 1,
            "resolution": resolution,
            "actuator_state": actuator_state,
        }
        signature = hmac.new(
            self.config.actuator_secret, canonical_json(body), hashlib.sha256
        ).hexdigest()
        return {**body, "signature": signature}

    def _result(
        self,
        envelope: Mapping[str, object],
        receipt_bytes: bytes,
        physical_observation: str,
    ) -> dict[str, Any]:
        receipt = json.loads(receipt_bytes)
        evidence_digest = sha256_digest(receipt_bytes)
        evidence_region_id = f"region://dogfood/receipt/{envelope['attempt_id']}"
        settlement = {
            **receipt,
            "dispatch_identity": envelope["dispatch_identity"],
            "evidence_region_id": evidence_region_id,
            "evidence_digest": evidence_digest,
            "proof_class": "ProviderConfirmation",
        }
        return {
            "receipt": receipt,
            "receipt_bytes": receipt_bytes,
            "evidence_region_id": evidence_region_id,
            "physical_observation": physical_observation,
            "settlement": settlement,
        }

    def acquire_and_process(
        self, attempt_id: int, dispatch_identity: str
    ) -> dict[str, Any]:
        acquired = self.actuator_client.request(
            "AcquireDispatch",
            {
                "attempt_id": attempt_id,
                "dispatch_identity": dispatch_identity,
                "actuator_id": self.config.actuator_id,
            },
        )
        if acquired.get("status") != "Ok" or not isinstance(
            acquired.get("outcome"), dict
        ):
            raise ActuatorError(f"AcquireDispatch failed: {acquired}")
        return self.process_envelope(acquired["outcome"])

    def settle(
        self, result: Mapping[str, object], publish_receipt: ReceiptPublisher
    ) -> None:
        publish_receipt(
            str(result["evidence_region_id"]),
            str(result["settlement"]["evidence_digest"]),
            result["receipt_bytes"],
        )
        settled = self.evidence_client.request(
            "PresentSettlementCertificate", result["settlement"]
        )
        outcome = settled.get("outcome")
        if (
            settled.get("status") != "Ok"
            or not isinstance(outcome, dict)
            or outcome.get("type") != "Settled"
            or outcome.get("resolution") != "Confirmed"
        ):
            raise ActuatorError(f"settlement rejected: {settled}")

    def acquire_and_settle(
        self,
        attempt_id: int,
        dispatch_identity: str,
        publish_receipt: ReceiptPublisher,
    ) -> dict[str, Any]:
        result = self.acquire_and_process(attempt_id, dispatch_identity)
        self.settle(result, publish_receipt)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Closed dogfood repository actuator")
    parser.add_argument("--target-workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--actuator-socket", type=Path, required=True)
    parser.add_argument("--evidence-socket", type=Path, required=True)
    parser.add_argument("--attempt-id", type=int, required=True)
    parser.add_argument("--dispatch-identity", required=True)
    parser.add_argument("--issuer", required=True)
    args = parser.parse_args()
    secret_hex = os.environ.get("CASTOR_ACTUATOR_SECRET_HEX", "")
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError as error:
        raise ActuatorError("invalid CASTOR_ACTUATOR_SECRET_HEX") from error
    config = ActuatorConfig(
        target_workspace=args.target_workspace,
        run_dir=args.run_dir,
        state_db=args.state_db,
        actuator_socket=args.actuator_socket,
        evidence_socket=args.evidence_socket,
        actuator_secret=secret,
        issuer=args.issuer,
    )
    RepoWorkspaceActuator(config).acquire_and_process(
        args.attempt_id, args.dispatch_identity
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
