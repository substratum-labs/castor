#!/usr/bin/env python3
"""Ring-3 reference policy for Castor cognitive recovery.

The policy consumes only Core-authored Turn admission observations and the
read-only recovery projection.  It may request the closed QueryOperation
interaction, but it never retries an effect, settles an Attempt, or submits an
operator decision.  Those safety transitions remain kernel/TCB responsibilities.

Example:
  python3 examples/cognitive_recovery_agent.py \
    --socket /run/castor/agent.sock \
    --admission admission.json --projection projection.json \
    --adapter-id c04:generic
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class RecoveryChoice:
    kind: str
    reason: str
    attempt: dict[str, Any] | None = None


class CognitiveRecoveryPolicy:
    """Bounded policy: at most one read-only probe per admitted Turn."""

    def __init__(self, adapter_id: str) -> None:
        if not adapter_id:
            raise ValueError("a registered adapter_id is required")
        self.adapter_id = adapter_id

    def choose(
        self, admission: dict[str, Any], projection: dict[str, Any]
    ) -> RecoveryChoice:
        snapshot = admission.get("unsettled_effects_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("author") != "Core":
            return RecoveryChoice("Yield", "missing Core-authored recovery snapshot")

        recovery = projection.get("recovery", {})
        if recovery.get("phase") == "Escalated":
            return RecoveryChoice("YieldToOperator", "kernel recovery is Escalated")
        if recovery.get("probe_budget_remaining", 0) <= 0:
            return RecoveryChoice("Yield", "kernel probe budget is exhausted")

        attempts = snapshot.get("attempts")
        if not isinstance(attempts, list):
            return RecoveryChoice("Yield", "invalid recovery snapshot")
        for attempt in sorted(attempts, key=lambda item: item.get("attempt_id", 0)):
            if (
                attempt.get("lock_state") == "WriteLocked_ProbeAllowed"
                and attempt.get("status") in {"ArmedUnknown", "Dispatched"}
                and isinstance(attempt.get("attempt_id"), int)
                and isinstance(attempt.get("stable_op_id"), str)
            ):
                return RecoveryChoice(
                    "Probe",
                    "unsettled effect permits one bounded observation this Turn",
                    attempt,
                )
        return RecoveryChoice("Continue", "no probe-eligible unsettled effects")

    def probe_request(
        self, choice: RecoveryChoice, interaction_id: str, lease_epoch: int = 0
    ) -> dict[str, Any]:
        if choice.kind != "Probe" or choice.attempt is None:
            raise ValueError("policy choice does not authorize a probe")
        attempt = choice.attempt
        descriptor = {
            "type": "QueryOperation",
            "attempt_id": attempt["attempt_id"],
            "stable_operation_id": attempt["stable_op_id"],
            "adapter_id": self.adapter_id,
        }
        return {
            "request_id": interaction_id,
            "op": "RequestInteraction",
            "payload": {
                "interaction_id": interaction_id,
                "lease_epoch": lease_epoch,
                "request_digest": "sha256:"
                + hashlib.sha256(canonical_json(descriptor)).hexdigest(),
                "descriptor": descriptor,
            },
        }


def call_castord(path: Path, request: dict[str, Any]) -> dict[str, Any]:
    payload = canonical_json(request)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.connect(str(path))
        stream.sendall(struct.pack(">I", len(payload)) + payload)
        size = struct.unpack(">I", _read_exact(stream, 4))[0]
        return json.loads(_read_exact(stream, size))


def _read_exact(stream: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise RuntimeError("castord closed an incomplete response")
        result.extend(chunk)
    return bytes(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--interaction-id", default="recovery-probe")
    parser.add_argument("--lease-epoch", type=int, default=0)
    parser.add_argument("--adapter-id", required=True)
    args = parser.parse_args()

    admission = json.loads(args.admission.read_text())
    projection = json.loads(args.projection.read_text())
    policy = CognitiveRecoveryPolicy(args.adapter_id)
    choice = policy.choose(admission, projection)
    output: dict[str, Any] = {"choice": choice.kind, "reason": choice.reason}
    if choice.kind == "Probe":
        if args.socket is None:
            parser.error("--socket is required when the policy selects Probe")
        request = policy.probe_request(choice, args.interaction_id, args.lease_epoch)
        output["response"] = call_castord(args.socket, request)
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
