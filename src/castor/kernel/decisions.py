"""Kernel security decisions — pure functions, zero I/O.

These functions encode the Kernel's security policy: authorization,
budget checks, HITL determination, and replay validation.  They read
state but never mutate it.

In castord (Rust daemon), this module becomes the Ring 0 state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from castor.gate.registry import ToolMetadata
from castor.models.capability import Capability
from castor.models.checkpoint import SyscallRecord


class ReplayDivergenceError(Exception):
    """Raised when a replay request doesn't match the recorded syscall."""

    def __init__(self, index: int, expected: dict, actual: dict):
        self.index = index
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Replay divergence at index {index}: expected {expected}, got {actual}"
        )


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


@dataclass
class ReplayHit:
    """Serve cached response from journal — no re-authorization needed."""

    response: Any
    new_replay_index: int


@dataclass
class Suspend:
    """Agent must suspend for human-in-the-loop approval."""

    request: dict[str, Any]


@dataclass
class Deny:
    """Syscall denied — return error response to agent."""

    response: dict[str, Any]


@dataclass
class Allow:
    """Syscall authorized — Scheduler should proceed with execution."""

    validated_args: dict[str, Any]
    tool_meta: ToolMetadata
    cost: float  # 0.0 means no budget tracking for this tool


SyscallDecision = ReplayHit | Suspend | Deny | Allow


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------


def decide_syscall(
    *,
    syscall_log: list[SyscallRecord],
    replay_index: int,
    kernel_tool_names: set[str],
    capabilities: dict[str, Capability],
    request: dict[str, Any],
    tool_meta: ToolMetadata,
    validated_args: dict[str, Any] | None,
    validation_error_response: dict[str, Any] | None,
) -> SyscallDecision:
    """Evaluate a syscall request and return a security decision.

    This is the Kernel's core logic — a pure function that reads state
    without mutating it.  The Scheduler (caller) is responsible for
    executing the returned decision.

    Args:
        syscall_log: The checkpoint's syscall journal.
        replay_index: Current position in the replay journal.
        kernel_tool_names: Tool names that are kernel-internal (skipped in replay).
        capabilities: The agent's current capability budgets.
        request: The raw syscall request dict (tool_name + arguments).
        tool_meta: Metadata for the requested tool.
        validated_args: Gate-validated arguments, or None if validation failed.
        validation_error_response: Formatted error dict if validation failed, else None.

    Returns:
        One of: ReplayHit, Suspend, Deny, Allow.

    Raises:
        ReplayDivergenceError: If replay request doesn't match recorded syscall.
    """
    # ── Phase 1: Replay check ──
    idx = replay_index

    # Skip kernel-internal records (side-effects baked into checkpoint)
    while idx < len(syscall_log):
        record = syscall_log[idx]
        if record.request.get("tool_name") not in kernel_tool_names:
            break
        idx += 1

    if idx < len(syscall_log):
        record = syscall_log[idx]
        if record.request != request:
            raise ReplayDivergenceError(idx, record.request, request)
        return ReplayHit(response=record.response, new_replay_index=idx + 1)

    # ── Phase 2: Validation check ──
    if validation_error_response is not None:
        return Deny(response=validation_error_response)

    # ── Phase 3: HITL determination ──
    # Destructive tools without budget tracking always need human approval
    # since there's no cap to limit them.
    always_hitl = tool_meta.requires_hitl or (
        tool_meta.destructive and tool_meta.cost_per_use <= 0
    )
    if always_hitl:
        return Suspend(request=request)

    # ── Phase 4: Budget check ──
    cost = tool_meta.cost_per_use
    if cost > 0:
        if not _budget_sufficient(capabilities, tool_meta.consumes, cost):
            if tool_meta.destructive:
                return Suspend(request=request)
            return Deny(
                response={
                    "status": "INSUFFICIENT_CAPABILITY",
                    "feedback_message": (
                        f"Capability exhausted: {tool_meta.consumes!r} — "
                        f"requested {cost}, remaining "
                        f"{_budget_remaining(capabilities, tool_meta.consumes)}"
                    ),
                }
            )

    # ── Phase 5: All clear ──
    return Allow(
        validated_args=validated_args or {},
        tool_meta=tool_meta,
        cost=cost,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _budget_sufficient(
    capabilities: dict[str, Capability], resource_type: str, cost: float
) -> bool:
    """Check if budget covers the cost. Missing resource = unlimited."""
    cap = capabilities.get(resource_type)
    if cap is None:
        return True
    return (cap.max_budget - cap.current_usage) >= cost


def _budget_remaining(capabilities: dict[str, Capability], resource_type: str) -> float:
    """Return remaining budget for a resource type."""
    cap = capabilities.get(resource_type)
    if cap is None:
        return float("inf")
    return cap.max_budget - cap.current_usage
