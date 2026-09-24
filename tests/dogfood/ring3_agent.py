"""Networkless Ring-3 participant for the governed coding dogfood trace."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from castor_client import AgentSession

MAX_CONTENT_BYTES = 65_536
ACTUATOR_ID = "repo-workspace-actuator"
FILE_PATHS = ("src/castor/ipc_client.py", "tests/test_ipc_client.py")
COMMAND_IDS = (
    "test_unittest",
    "test_pytest",
    "lint_check",
    "format_check",
    "baseline_rust",
    "baseline_castord_python",
)


class ProtocolError(RuntimeError):
    """A fail-closed AISA framing or response error."""


class AgentPhase(Enum):
    REQUEST_MODEL = "request_model"
    CONSUME_MODEL = "consume_model"
    COMMIT_ACTIONS = "commit_actions"
    REGISTER_ACTIONS = "register_actions"
    DRIVE_EFFECTS = "drive_effects"
    OBSERVE_SETTLEMENTS = "observe_settlements"
    COMPLETE = "complete"


@dataclass(frozen=True)
class AgentConfig:
    socket_path: Path
    agent_id: str
    turn_id: int
    lease_epoch: int
    consume_lease_epoch: int
    base_projection_digest: str
    capability_id: str
    generation: int
    interaction_id: str
    request_digest: str
    interaction_timeout_seconds: float = 600.0
    poll_interval_seconds: float = 0.1

    @classmethod
    def environment_keys(cls) -> tuple[str, ...]:
        return (
            "CASTOR_IPC_SOCKET",
            "CASTOR_AGENT_ID",
            "CASTOR_TURN_ID",
            "CASTOR_LEASE_EPOCH",
            "CASTOR_CONSUME_LEASE_EPOCH",
            "CASTOR_BASE_PROJECTION_DIGEST",
            "CASTOR_CAPABILITY_ID",
            "CASTOR_GENERATION",
            "CASTOR_INTERACTION_ID",
            "CASTOR_REQUEST_DIGEST",
        )

    @classmethod
    def from_env(cls) -> AgentConfig:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ProtocolError(f"missing required environment variable {name}")
            return value

        try:
            return cls(
                socket_path=Path(required("CASTOR_IPC_SOCKET")),
                agent_id=required("CASTOR_AGENT_ID"),
                turn_id=int(required("CASTOR_TURN_ID")),
                lease_epoch=int(os.environ.get("CASTOR_LEASE_EPOCH", "0")),
                consume_lease_epoch=int(
                    os.environ.get("CASTOR_CONSUME_LEASE_EPOCH", "1")
                ),
                base_projection_digest=required("CASTOR_BASE_PROJECTION_DIGEST"),
                capability_id=required("CASTOR_CAPABILITY_ID"),
                generation=int(required("CASTOR_GENERATION")),
                interaction_id=required("CASTOR_INTERACTION_ID"),
                request_digest=required("CASTOR_REQUEST_DIGEST"),
                interaction_timeout_seconds=float(
                    os.environ.get("CASTOR_INTERACTION_TIMEOUT_SECONDS", "600")
                ),
                poll_interval_seconds=float(
                    os.environ.get("CASTOR_POLL_INTERVAL_SECONDS", "0.1")
                ),
            )
        except ValueError as error:
            raise ProtocolError("numeric agent configuration is invalid") from error


@dataclass(frozen=True)
class Action:
    action_id: str
    stable_operation_id: str
    target_scope: str
    payload_region_ref: str
    payload_digest: str
    payload: bytes


@dataclass(frozen=True)
class AgentRun:
    actions: tuple[Action, ...]
    attempt_ids: tuple[int, ...]


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Ring3Agent:
    def __init__(self, config: AgentConfig, client: AgentSession | None = None) -> None:
        if config.lease_epoch != 0:
            raise ProtocolError("new Turn admission requires lease_epoch=0")
        self.config = config
        self.client = client or AgentSession(config.socket_path)
        self.phase = AgentPhase.REQUEST_MODEL

    def _expect(
        self, op: str, payload: dict[str, object], *types: str
    ) -> dict[str, Any]:
        response = self.client.request(op, payload)
        if response.get("status") != "Ok":
            error = response.get("error")
            raise ProtocolError(f"{op} failed: {error}")
        outcome = response.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("type") not in types:
            raise ProtocolError(f"{op} returned unexpected outcome: {outcome}")
        return outcome

    def run(self) -> AgentRun:
        config = self.config
        self._expect(
            "AdmitTurn",
            {
                "agent_id": config.agent_id,
                "turn_id": config.turn_id,
                "lease_epoch": 0,
                "base_projection_digest": config.base_projection_digest,
                "cap_id": config.capability_id,
            },
            "Admitted",
        )
        self._expect(
            "RequestInteraction",
            {
                "interaction_id": config.interaction_id,
                "lease_epoch": 0,
                "request_digest": config.request_digest,
            },
            "InteractionRequested",
        )

        self.phase = AgentPhase.CONSUME_MODEL
        consumed = self._consume_model()
        completion = self._decode_completion(consumed.get("payload"))
        actions = self._build_actions(completion)

        self.phase = AgentPhase.COMMIT_ACTIONS
        for action in actions:
            self._ensure_region(
                action.payload_region_ref, action.payload_digest, action.payload
            )
        successor = canonical_json(completion)
        successor_digest = sha256_digest(successor)
        successor_ref = f"region://dogfood/successor/{successor_digest[7:]}"
        manifest_bytes = (
            "\n".join(action.action_id for action in actions) + "\n"
        ).encode("utf-8")
        manifest_digest = sha256_digest(manifest_bytes)
        manifest_ref = f"region://dogfood/manifest/{manifest_digest[7:]}"
        self._ensure_region(successor_ref, successor_digest, successor)
        self._ensure_region(manifest_ref, manifest_digest, manifest_bytes)
        self._expect(
            "CommitTurn",
            {
                "lease_epoch": config.consume_lease_epoch,
                "base_projection_digest": config.base_projection_digest,
                "successor_region_id": successor_ref,
                "successor_digest": successor_digest,
                "action_manifest_region_id": manifest_ref,
                "action_manifest_digest": manifest_digest,
                "action_manifest": [action.action_id for action in actions],
                "action_bindings": [
                    {
                        "action_id": action.action_id,
                        "payload_region_ref": action.payload_region_ref,
                        "payload_digest": action.payload_digest,
                        "actuator_id": ACTUATOR_ID,
                    }
                    for action in actions
                ],
                "cap_id": config.capability_id,
            },
            "TurnCommitted",
        )

        self.phase = AgentPhase.REGISTER_ACTIONS
        for action in actions:
            self._expect(
                "RegisterAction",
                {
                    "stable_operation_id": action.stable_operation_id,
                    "action_id": action.action_id,
                    "agent_id": config.agent_id,
                    "action_family": ACTUATOR_ID,
                    "cap_id": config.capability_id,
                    "target_scope": action.target_scope,
                },
                "ActionRegistered",
            )

        self.phase = AgentPhase.DRIVE_EFFECTS
        attempt_ids: list[int] = []
        for action in actions:
            armed = self._expect(
                "PresentAdmissionCertificate",
                {
                    "action_id": action.action_id,
                    "target_scope": action.target_scope,
                    "capability_id": config.capability_id,
                    "generation": config.generation,
                },
                "AttemptArmed",
            )
            attempt_id = armed.get("attempt_id")
            if not isinstance(attempt_id, int):
                raise ProtocolError("AttemptArmed is missing attempt_id")
            self._expect(
                "RecordDispatchAttempt",
                {
                    "attempt_id": attempt_id,
                    "dispatch_identity": action.stable_operation_id,
                },
                "DispatchRecorded",
            )
            attempt_ids.append(attempt_id)

        self.phase = AgentPhase.OBSERVE_SETTLEMENTS
        return AgentRun(actions=actions, attempt_ids=tuple(attempt_ids))

    def observe_settlements(self, turn_id: int, base_projection_digest: str) -> bool:
        """Admit a later Turn and complete only when Core reports no attempts."""
        if self.phase is not AgentPhase.OBSERVE_SETTLEMENTS:
            raise ProtocolError("settlements can only be observed after dispatch")
        admitted = self._expect(
            "AdmitTurn",
            {
                "agent_id": self.config.agent_id,
                "turn_id": turn_id,
                "lease_epoch": 0,
                "base_projection_digest": base_projection_digest,
                "cap_id": self.config.capability_id,
            },
            "Admitted",
        )
        snapshot = admitted.get("unsettled_effects_snapshot")
        if not isinstance(snapshot, dict):
            raise ProtocolError("unsettled effects snapshot must be an object")
        if set(snapshot) != {"author", "turn_id", "region_ref", "attempts"}:
            raise ProtocolError("unsettled effects snapshot fields are invalid")
        if (
            snapshot["author"] != "Core"
            or snapshot["turn_id"] != turn_id
            or snapshot["region_ref"] != f"observation:unsettled_effects:{turn_id}"
        ):
            raise ProtocolError("unsettled effects snapshot identity is invalid")
        attempts = snapshot["attempts"]
        if not isinstance(attempts, list):
            raise ProtocolError("unsettled effects snapshot must contain attempts")
        for attempt in attempts:
            self._validate_unsettled_attempt(attempt)
        self._commit_observation_turn(snapshot, base_projection_digest)
        if attempts:
            return False
        self.phase = AgentPhase.COMPLETE
        return True

    @staticmethod
    def _validate_unsettled_attempt(attempt: object) -> None:
        fields = {
            "attempt_id",
            "action_id",
            "target_scope",
            "status",
            "stable_op_id",
            "lock_state",
            "ambiguous_delivery",
        }
        if not isinstance(attempt, dict) or set(attempt) != fields:
            raise ProtocolError("unsettled Attempt fields are invalid")
        if (
            not isinstance(attempt["attempt_id"], int)
            or isinstance(attempt["attempt_id"], bool)
            or attempt["attempt_id"] <= 0
            or not all(
                isinstance(attempt[name], str) and attempt[name]
                for name in ("action_id", "target_scope", "status", "lock_state")
            )
            or attempt["status"]
            not in {"ArmedUnknown", "Dispatched", "QuarantinedDispute"}
            or (
                attempt["stable_op_id"] is not None
                and not isinstance(attempt["stable_op_id"], str)
            )
            or not isinstance(attempt["ambiguous_delivery"], bool)
        ):
            raise ProtocolError("unsettled Attempt values are invalid")

    def _commit_observation_turn(
        self, snapshot: dict[str, object], base_projection_digest: str
    ) -> None:
        successor = canonical_json(snapshot)
        successor_digest = sha256_digest(successor)
        successor_ref = f"region://dogfood/observation/{successor_digest[7:]}"
        manifest = b""
        manifest_digest = sha256_digest(manifest)
        manifest_ref = f"region://dogfood/manifest/{manifest_digest[7:]}"
        self._ensure_region(successor_ref, successor_digest, successor)
        self._ensure_region(manifest_ref, manifest_digest, manifest)
        self._expect(
            "CommitTurn",
            {
                "lease_epoch": 0,
                "base_projection_digest": base_projection_digest,
                "successor_region_id": successor_ref,
                "successor_digest": successor_digest,
                "action_manifest_region_id": manifest_ref,
                "action_manifest_digest": manifest_digest,
                "action_manifest": [],
                "action_bindings": [],
                "cap_id": self.config.capability_id,
            },
            "TurnCommitted",
        )

    @classmethod
    def resume_settlement_observation(cls, config: AgentConfig) -> bool:
        agent = cls(config)
        agent.phase = AgentPhase.OBSERVE_SETTLEMENTS
        return agent.observe_settlements(config.turn_id, config.base_projection_digest)

    def _ensure_region(
        self, region_ref: str, content_digest: str, content: bytes
    ) -> None:
        self._expect(
            "EnsureRegion",
            {
                "region_ref": region_ref,
                "content_digest": content_digest,
                "content": list(content),
            },
            "Success",
            "AlreadyPersistedSameContent",
        )

    def _consume_model(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.interaction_timeout_seconds
        request = {
            "interaction_id": self.config.interaction_id,
            "lease_epoch": self.config.consume_lease_epoch,
        }
        while True:
            response = self.client.request("ConsumeInteraction", request)
            if response.get("status") != "Ok":
                raise ProtocolError(
                    f"ConsumeInteraction failed: {response.get('error')}"
                )
            outcome = response.get("outcome")
            if (
                isinstance(outcome, dict)
                and outcome.get("type") == "InteractionConsumed"
            ):
                return outcome
            if not isinstance(outcome, dict) or outcome.get("type") not in {
                "RejectedPrecondition",
                "RejectedStaleAuthority",
                "UnavailableBeforeAck",
            }:
                raise ProtocolError(
                    f"ConsumeInteraction returned unexpected outcome: {outcome}"
                )
            if time.monotonic() >= deadline:
                raise ProtocolError("timed out waiting for the model Interaction")
            time.sleep(self.config.poll_interval_seconds)

    def _decode_completion(self, payload: object) -> dict[str, object]:
        expected_fields = {
            "interaction_id",
            "observation_region_id",
            "observation_digest",
            "content",
            "lease_epoch",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ProtocolError(
                "Interaction payload fields do not match the wire schema"
            )
        if (
            payload["interaction_id"] != self.config.interaction_id
            or payload["lease_epoch"] != self.config.consume_lease_epoch
        ):
            raise ProtocolError("Interaction payload identity or lease mismatch")
        content = payload["content"]
        if not isinstance(content, list) or any(
            not isinstance(value, int) or not 0 <= value <= 255 for value in content
        ):
            raise ProtocolError("Interaction content must be a byte array")
        content_bytes = bytes(content)
        if payload["observation_digest"] != sha256_digest(content_bytes):
            raise ProtocolError("Interaction observation digest mismatch")
        try:
            value = json.loads(content_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(
                "Interaction payload is not valid UTF-8 JSON"
            ) from error
        if not isinstance(value, dict):
            raise ProtocolError("model completion must be an object")
        files = value.get("files")
        if not isinstance(files, list) or len(files) != len(FILE_PATHS):
            raise ProtocolError("model completion must contain exactly two files")
        return value

    def _build_actions(self, completion: dict[str, object]) -> tuple[Action, ...]:
        if set(completion) != {"schema_version", "files", "explanation"}:
            raise ProtocolError(
                "model completion fields do not match the closed schema"
            )
        if completion["schema_version"] != 1 or not isinstance(
            completion["explanation"], str
        ):
            raise ProtocolError(
                "model completion schema version or explanation is invalid"
            )
        files = completion["files"]
        assert isinstance(files, list)
        actions: list[Action] = []
        for index, expected_path in enumerate(FILE_PATHS):
            item = files[index]
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "content_utf8"}
                or item.get("path") != expected_path
            ):
                raise ProtocolError(
                    "model completion paths are not the closed ordered set"
                )
            content = item.get("content_utf8")
            if not isinstance(content, str):
                raise ProtocolError("model completion content_utf8 must be a string")
            if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
                raise ProtocolError("model completion exceeds 65,536 UTF-8 bytes")
            payload = canonical_json(
                {"kind": "write_file", "path": expected_path, "content_utf8": content}
            )
            actions.append(
                self._action(
                    f"write-file-{index + 1}",
                    f"repo:castor:file/{expected_path}",
                    payload,
                )
            )
        for command_id in COMMAND_IDS:
            actions.append(
                self._action(
                    f"command-{command_id}",
                    f"repo:castor:cmd/{command_id}",
                    canonical_json({"kind": "run_command", "command_id": command_id}),
                )
            )
        return tuple(actions)

    def _action(self, action_id: str, target_scope: str, payload: bytes) -> Action:
        payload_digest = sha256_digest(payload)
        turn_action_id = f"turn-{self.config.turn_id}-{action_id}"
        return Action(
            action_id=turn_action_id,
            stable_operation_id=f"dogfood-{self.config.turn_id}-{action_id}",
            target_scope=target_scope,
            payload_region_ref=f"region://dogfood/payload/{payload_digest[7:]}",
            payload_digest=payload_digest,
            payload=payload,
        )


def main() -> int:
    config = AgentConfig.from_env()
    mode = os.environ.get("CASTOR_AGENT_MODE", "publish")
    if mode == "publish":
        Ring3Agent(config).run()
    elif mode == "observe_settlements":
        if not Ring3Agent.resume_settlement_observation(config):
            return 3
    else:
        raise ProtocolError(f"unsupported CASTOR_AGENT_MODE: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
