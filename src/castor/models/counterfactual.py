"""Counterfactual replay types.

Supports replaying a recorded session with one or more decisions
overridden at chosen syscall boundaries, observing how the trajectory
diverges. This is Castor's primary differentiator — no competitor
exposes a primitive to change a decision and re-run downstream live.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class SyscallOverride(BaseModel):
    """One override applied at a specific syscall boundary in a replay.

    Replaces what the syscall WOULD have returned with
    ``replacement_output``. The agent sees ``replacement_output`` as
    if the underlying tool or LLM had produced it.
    """

    replacement_output: Any
    note: str = ""


class CounterfactualRecord(BaseModel):
    """One override applied during a counterfactual replay.

    Stored in ``AgentCheckpoint.counterfactual_log``. Replay of the CF
    session re-applies these overrides at the same invocation_ids so
    the CF session itself is deterministically reproducible.
    """

    invocation_id: str
    syscall_index: int
    original_output_digest: str
    replacement_output: Any
    note: str = ""
    timestamp: float = 0.0


class ReplayMode(StrEnum):
    """Cost / fidelity tradeoff for counterfactual replay."""

    LIVE_FROM_DIVERGENCE = "live_from_divergence"
    """From override step onward, all syscalls run live.
    Highest fidelity. Costs ~half a fresh run."""

    REPLAY_WHEN_ARGS_MATCH = "replay_when_args_match"
    """Opportunistic: if downstream args match the parent journal
    at the same invocation_id, replay cached; else go live."""

    REPLAY_ALL = "replay_all"
    """Fiction mode: replay every later step from journal even when
    args diverge. ~Free but trajectory may be incoherent."""


class CounterfactualResult(BaseModel):
    """Result of a counterfactual replay."""

    session_id: str
    parent_session_id: str
    diverged_at_step: int
    overrides_applied: list[CounterfactualRecord]
    final_status: str
    total_cost: float = 0.0
    total_steps: int = 0


# ── Exceptions ──


class CounterfactualError(Exception):
    """Base for all counterfactual replay errors."""


class OverrideNotAllowedError(CounterfactualError):
    """Override targets a disallowed syscall (spawn/fork/etc.)."""


class OverrideTypeMismatchError(CounterfactualError):
    """replacement_output's type doesn't match recorded return type."""


class OverrideTargetNotFoundError(CounterfactualError):
    """Neither invocation_id nor syscall_index resolves to a real
    entry in the parent session's journal."""


def digest_output(output: Any) -> str:
    """SHA-256 digest of a syscall's recorded output.

    Used in ``CounterfactualRecord.original_output_digest`` to detect
    parent journal tampering without storing the full output.
    """
    serialized = json.dumps(output, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:32]
