"""Minimal D1 Agent using only an installed castor-client wheel and agent.sock."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import castor_client
from castor_client import AgentSession


def main() -> None:
    package_path = Path(castor_client.__file__).resolve()
    if not package_path.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Agent did not import the installed client wheel")
    config = json.loads(os.environ["CASTOR_A_TRACE_CONFIG"])
    agent = AgentSession(config["socket"])
    outcomes: list[str] = []

    def request(op: str, payload: dict[str, object], *expected: str) -> dict:
        response = agent.request(op, payload)
        outcome = response["outcome"]
        kind = outcome.get("type")
        if kind not in expected:
            raise RuntimeError(f"{op} returned {kind!r}, expected {expected!r}")
        return outcome

    request(
        "AdmitTurn",
        {
            "agent_id": config["agent_id"],
            "turn_id": config["turn_id"],
            "lease_epoch": 0,
            "base_projection_digest": config["base_projection_digest"],
            "cap_id": config["cap_id"],
        },
        "Admitted",
    )
    outcomes.append("Admitted")
    request(
        "RequestInteraction",
        {
            "interaction_id": config["interaction_id"],
            "lease_epoch": 0,
            "request_digest": config["request_digest"],
        },
        "InteractionRequested",
    )
    outcomes.append("InteractionRequested")

    deadline = time.monotonic() + 10
    while True:
        observed = request(
            "ConsumeInteraction",
            {"interaction_id": config["interaction_id"], "lease_epoch": 1},
            "InteractionConsumed",
            "RejectedPrecondition",
            "RejectedStaleAuthority",
            "UnavailableBeforeAck",
        )
        if observed["type"] == "InteractionConsumed":
            payload = observed.get("payload")
            if not isinstance(payload, dict) or (
                payload.get("observation_digest") != config["observation_digest"]
            ):
                raise RuntimeError("model observation binding was not preserved")
            outcomes.append("InteractionConsumed")
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("model Interaction did not become consumable")
        time.sleep(0.02)

    for name in ("successor", "manifest", "payload"):
        region = config[name]
        request(
            "EnsureRegion",
            {
                "region_ref": region["ref"],
                "content_digest": region["digest"],
                "content": region["content"],
            },
            "Success",
            "AlreadyPersistedSameContent",
        )

    request(
        "CommitTurn",
        {
            "lease_epoch": 1,
            "base_projection_digest": config["base_projection_digest"],
            "successor_region_id": config["successor"]["ref"],
            "successor_digest": config["successor"]["digest"],
            "action_manifest_region_id": config["manifest"]["ref"],
            "action_manifest_digest": config["manifest"]["digest"],
            "action_manifest": [config["action_id"]],
            "action_bindings": [
                {
                    "action_id": config["action_id"],
                    "payload_region_ref": config["payload"]["ref"],
                    "payload_digest": config["payload"]["digest"],
                    "actuator_id": config["actuator_id"],
                }
            ],
            "cap_id": config["cap_id"],
        },
        "TurnCommitted",
    )
    outcomes.append("TurnCommitted")
    request(
        "RegisterAction",
        {
            "action_id": config["action_id"],
            "stable_operation_id": config["stable_operation_id"],
            "agent_id": config["agent_id"],
            "action_family": config["actuator_id"],
            "cap_id": config["cap_id"],
            "target_scope": config["target_scope"],
        },
        "ActionRegistered",
    )
    outcomes.append("ActionRegistered")
    armed = request(
        "PresentAdmissionCertificate",
        {
            "action_id": config["action_id"],
            "target_scope": config["target_scope"],
            "capability_id": config["cap_id"],
            "generation": config["generation"],
        },
        "AttemptArmed",
    )
    attempt_id = armed.get("attempt_id")
    if not isinstance(attempt_id, int) or attempt_id <= 0:
        raise RuntimeError("Core did not return a valid attempt ID")
    outcomes.append("AttemptArmed")
    request(
        "RecordDispatchAttempt",
        {
            "attempt_id": attempt_id,
            "dispatch_identity": config["stable_operation_id"],
        },
        "DispatchRecorded",
    )
    outcomes.append("DispatchRecorded")
    print(json.dumps(outcomes), flush=True)


if __name__ == "__main__":
    main()
