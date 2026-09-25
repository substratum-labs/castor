"""Host-only setup for the installed-wheel D1 Agent physical trace."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tests.test_cognitive_recovery_castord import (
    AGENT,
    CAP,
    OP_ID,
    digest,
)

if TYPE_CHECKING:
    import subprocess

    from tests.test_cognitive_recovery_castord import Daemon


def prepare_trace(
    daemon: Daemon, *, action_payload: bytes = b"payload-a1"
) -> tuple[dict[str, object], tuple[str, str]]:
    grant = {
        "cap_id": CAP,
        "subject": AGENT,
        "object_ref": daemon.adapter_id,
        "rights": ["AdmitTurn", "RegisterAction"],
        "constraints": [],
        "parent_cap_id": None,
        "revocation_domain": None,
        "delegation_allowed": False,
        "max_turns": None,
    }
    daemon.ok("GrantCapability", {"grant": grant}, "CapabilityGranted", "control")
    observation = daemon.region("observation", b"model-result")

    def region(name: str, content: bytes) -> dict[str, object]:
        return {
            "ref": f"region://recovery/{name}",
            "digest": digest(content),
            "content": list(content),
        }

    config: dict[str, object] = {
        "socket": str(daemon.agent),
        "agent_id": AGENT,
        "turn_id": 1,
        "base_projection_digest": daemon.base,
        "cap_id": CAP,
        "interaction_id": "installed-wheel-model",
        "request_digest": digest(b"installed-wheel-request"),
        "observation_digest": observation[1],
        "successor": region("successor", b"successor-state"),
        "manifest": region("manifest", b"a1\n"),
        "payload": region("payload-a1", action_payload),
        "action_id": "a1",
        "actuator_id": daemon.adapter_id,
        "target_scope": daemon.target_scope,
        "stable_operation_id": OP_ID,
        "generation": daemon.generation,
    }
    return config, observation


def report_model_after_request(
    daemon: Daemon,
    process: subprocess.Popen[str],
    interaction_id: str,
    observation: tuple[str, str],
) -> tuple[str, str]:
    deadline = time.monotonic() + 10
    while not any("InteractionRequested" in entry for entry in daemon.journal()):
        if process.poll() is not None:
            _, stderr = process.communicate(timeout=2)
            raise AssertionError(f"installed Agent exited early: {stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError(
                "installed Agent did not request the model Interaction"
            )
        time.sleep(0.02)
    daemon.report(interaction_id, observation)
    return process.communicate(timeout=15)
