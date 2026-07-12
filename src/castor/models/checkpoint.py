"""Checkpoint/Replay data models for the Castor Stream scheduler.

State Taxonomy
--------------
Every field on ``AgentCheckpoint`` and ``SyscallRecord`` is classified
into one of three semantic categories:

**Control truth** — fields whose consistency is *required* for correct
execution semantics: ownership, lifecycle, budget authority, pause/resume
intent, committed replay cursor. In a future distributed (Orrery)
deployment these must be strongly consistent across nodes.

**Execution state** — fields that capture *what happened*: the syscall
journal, context history, terminal result, and intermediate artifacts.
These can tolerate eventual consistency; the journal is the authoritative
record of past execution, but its growth is proportional to agent work.

**Informational** — fields that support debugging, observability, or
human experience but are *never* consulted for execution decisions.
Loss or staleness of these fields does not affect correctness.

This classification is a design constraint, not a runtime enforcement
(yet). It prepares the ground for Orrery, where the control plane and
data plane will be physically separated.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from castor.models.budget import Budget
from castor.models.preemption import PreemptionReason


class SyscallPurpose(StrEnum):
    """Classification of a syscall's role in the agent's execution.

    Used for cost accounting: budget dashboards can separate the agent's
    primary work from the kernel's memory management overhead.

    Closed set — adding values requires a contract change in
    ``castor-internal/contracts/memory.md``.
    """

    TASK_EXECUTION = "task_execution"
    """Default — the agent's main work (tool calls, LLM inference)."""

    MEMORY_MANAGEMENT = "memory_management"
    """mem_* syscalls and any LLM calls triggered by memory summarization."""

    INTROSPECTION = "introspection"
    """Status, budget, and capability queries (future)."""


class CastorMessage(BaseModel):
    """A message in the agent's context history with MMU metadata.

    Classification: **execution state** — part of the agent's working
    memory, managed by the MMU, and persisted in the checkpoint.

    Every message carries a stable ``id`` (content-addressable hash)
    that survives checkpoint/replay and serves as the addressing key
    for all ``mem_*`` syscalls.
    """

    id: str = ""
    """Stable, replay-safe identity. Computed at creation time via
    ``compute_memory_id(pid, seq, role, content)``. Empty string for
    messages created before this field was introduced (backwards compat).
    """

    role: str
    content: Any
    pinned: bool = False  # Never evicted by MMU if True
    token_count: int = 0  # 0 = use estimator


class SyscallRecord(BaseModel):
    """One entry in the execution journal (syscall_log).

    Each record captures a single tool invocation: the request that was
    dispatched, the response that came back, and metadata about HITL and
    speculative review. The journal is append-only during a run and is
    the authoritative source for replay.

    Classification: **execution state** — the journal is the core of
    replay, but its individual entries are immutable facts, not live
    control state.
    """

    # ── Execution state ──
    request: dict[str, Any]
    """The syscall request: ``{tool_name, arguments}``."""

    response: Any
    """The tool's return value (or error)."""

    purpose: SyscallPurpose = SyscallPurpose.TASK_EXECUTION
    """Classification for cost accounting.

    Default ``TASK_EXECUTION`` for backwards compatibility — old records
    without the field deserialize to this value. The kernel sets
    ``MEMORY_MANAGEMENT`` when dispatching mem_* syscalls and any LLM
    calls triggered by memory summarization.
    """

    invocation_id: str | None = None
    """Deterministic operation identity for this invocation.

    Computed as ``sha256(pid || syscall_index)`` at dispatch time. Stable
    across replay even when prompt-derived tool arguments vary. Enables
    idempotent retry and external side-effect reconciliation.

    ``None`` for records created before this field was introduced
    (backwards compatible).
    """

    # ── Informational ──
    was_hitl: bool = False
    """True if this invocation was approved by a human."""

    needs_review: bool = False
    """True if flagged for post-hoc review (speculative execution)."""

    review_reason: str | None = None
    """Why this invocation was flagged (speculative execution)."""

    # ── Execution state (nested) ──
    child_checkpoint: AgentCheckpoint | None = None
    """For spawn/join: the child agent's checkpoint at completion."""


class PreemptionRecord(BaseModel):
    """One preemption event in the agent's history.

    Stored in ``AgentCheckpoint.preemption_log``. On replay, the runner
    re-injects the preempt at the same syscall boundary so replays are
    byte-identical.
    """

    syscall_index_after: int
    """Preemption fires AFTER this syscall completes (before the next)."""

    reason: PreemptionReason
    timestamp: float
    """Wall-clock for diagnostics; NOT used in replay logic."""

    metadata: dict[str, Any] = Field(default_factory=dict)


def compute_invocation_id(
    pid: str,
    syscall_index: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Compute a deterministic operation identity from a journal position.

    The identity is derived only from ``pid || syscall_index``.  ``tool_name``
    and ``arguments`` remain accepted for source compatibility with the
    original API, but are deliberately excluded: an LLM may change wording or
    tool arguments while catch-up replay must retain the same external-effect
    identity for the journaled operation.

    Returns a hex-encoded SHA-256 truncated to 32 characters for
    compactness. Collisions are astronomically unlikely at this length
    for the expected cardinality (millions of invocations, not billions).
    """
    del tool_name, arguments
    payload = f"{pid}|{syscall_index}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def compute_memory_id(
    pid: str,
    seq: int,
    role: str,
    content: str,
) -> str:
    """Compute a deterministic, content-addressable message identity.

    Used as the ``CastorMessage.id`` for all ``mem_*`` syscall addressing.
    The hash is over ``pid || seq || role || content`` so that:

    - The same execution path always produces the same IDs (replay-stable).
    - Different pids (e.g. after a fork) produce different IDs.
    - Content changes produce different IDs (content-addressable).

    ``seq`` is the message's position in context_history at creation time.
    """
    payload = f"{pid}|{seq}|{role}|{content}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class AgentCheckpoint(BaseModel):
    """Complete execution state of a Castor agent process.

    This is the unit of persistence, replay, fork, and migration.
    Every field is classified below to guide future separation of
    the control plane (strongly consistent) from the data plane
    (eventually consistent).
    """

    # ── Control truth ──
    # These fields determine *who runs* and *what authority they have*.

    pid: str
    """Unique process identity. Deterministic for spawned children
    (``{parent_pid}::{agent_name}-{spawn_count}``)."""

    parent_pid: str | None = None
    """Parent that spawned this agent (``None`` for root)."""

    status: Literal[
        "RUNNING",
        "SUSPENDED_FOR_HITL",
        "PREEMPTED",
        "SUSPENDED",
        "KILLED",
        "COMPLETED",
        "FAILED",
        "BUDGET_EXHAUSTED",
    ]
    """Lifecycle state. Transitions enforced by the runner.

    RUNNING → PREEMPTED → SUSPENDED → RUNNING (resume) or → KILLED.
    BUDGET_EXHAUSTED is a legacy alias for PREEMPTED + reason=budget.
    """

    priority: int = 5
    """Scheduling priority (1=lowest, 10=highest). Inherited from spawn
    args. Used by the scheduler to order child dispatch: higher priority
    children are dispatched first, ties broken by creation order."""

    agent_function_name: str
    """Registered name of the agent function to execute."""

    capabilities: dict[str, Budget]
    """Budget authority. The kernel *enforces* these limits — they are
    not advisory. Delegated from parent on spawn, reclaimed on join."""

    pending_hitl: dict[str, Any] | None = None
    """The syscall request that caused suspension. ``None`` when not
    suspended. Set by the kernel's Suspend decision, cleared by
    approve/reject/modify."""

    pending_commit: tuple[str, int] | None = None
    """Provisional ``(pid, syscall_index)`` for an unjournaled external effect.

    A resumed runner queries its actuator before re-entering agent code. Formal
    operation-ID injection and result reconciliation are deliberately deferred.
    """

    pending_commit_status: str | None = None
    """Most recent actuator reconciliation result for ``pending_commit``."""

    preemption_log: list[PreemptionRecord] = Field(default_factory=list)
    """Append-only log of preemption events. Each entry records
    ``syscall_index_after`` so replay re-injects preempts at the
    exact same point."""

    # ── Counterfactual replay ──

    counterfactual_log: list[Any] = Field(default_factory=list)
    """Overrides applied during a counterfactual replay. Each entry
    is a ``CounterfactualRecord`` (imported as Any to avoid circular).
    Replay of the CF session re-applies these at the same points."""

    parent_session_id: str | None = None
    """The session this was forked from for counterfactual replay.
    ``None`` for non-CF sessions."""

    diverged_at_step: int | None = None
    """The syscall index where the counterfactual override was first
    applied. ``None`` for non-CF sessions."""

    # ── Execution state ──
    # These fields capture *what happened*. They grow with agent work.

    syscall_log: list[SyscallRecord] = Field(default_factory=list)
    """Append-only execution journal. The replay cursor is implicit
    (``len(syscall_log)`` after replay catch-up). Each entry is a
    committed fact that was either executed live or replayed from cache."""

    context_history: list[CastorMessage | dict[str, Any]] = Field(
        default_factory=list,
    )
    """Agent's working memory (messages). Managed by the MMU: subject
    to eviction, pinning, and page-out. Plain dicts in this list are
    user-layer data that bypass MMU eviction."""

    result: Any | None = None
    """Terminal return value of the agent function. Set on COMPLETED."""

    # ── Informational ──
    # Loss of these fields does not affect execution correctness.

    preemption_reason: str | None = None
    """Human-readable reason for preemption (e.g. "user cancelled")."""

    preemption_payload: dict[str, Any] | None = None
    """Arbitrary data attached by the preemptor."""

    partial_work: str | None = None
    """Accumulated streaming output at the time of preemption. Useful
    for showing the user what the agent was working on when stopped."""

    # ── Convenience properties ──

    @property
    def pending_tool(self) -> str | None:
        """Name of the tool awaiting HITL approval."""
        if self.pending_hitl is None:
            return None
        return self.pending_hitl.get("tool_name")

    @property
    def pending_args(self) -> dict[str, Any] | None:
        """Arguments of the tool awaiting HITL approval."""
        if self.pending_hitl is None:
            return None
        return self.pending_hitl.get("arguments")

    def budget_used(self, resource: str) -> float:
        """Current usage for a resource type (0.0 if not tracked)."""
        cap = self.capabilities.get(resource)
        return cap.current_usage if cap else 0.0

    def budget_remaining(self, resource: str) -> float:
        """Remaining budget for a resource type (0.0 if not tracked)."""
        cap = self.capabilities.get(resource)
        return (cap.max_budget - cap.current_usage) if cap else 0.0

    def fork(self, *, at_step: int) -> AgentCheckpoint:
        """Fork a new checkpoint from this one, rewinding to a specific step.

        Keeps syscall_log[:at_step] (cached for replay), discards the rest.
        The forked checkpoint is RUNNING with no result, ready for re-execution.

        Args:
            at_step: Keep steps 0..at_step-1, discard at_step onwards.

        Returns:
            A new independent checkpoint forked from this one.
        """
        if at_step < 0 or at_step > len(self.syscall_log):
            raise ValueError(
                f"at_step={at_step} out of range (0..{len(self.syscall_log)})"
            )
        forked = self.model_copy(deep=True)
        forked.syscall_log = forked.syscall_log[:at_step]
        forked.status = "RUNNING"
        forked.result = None
        forked.pending_hitl = None
        forked.pending_commit = None
        forked.pending_commit_status = None
        forked.preemption_reason = None
        forked.preemption_payload = None
        forked.partial_work = None
        forked.pid = f"{self.pid}::fork-{at_step}"
        return forked

    @property
    def is_suspended(self) -> bool:
        """True if agent is waiting for human-in-the-loop approval."""
        return self.status == "SUSPENDED_FOR_HITL"

    @property
    def is_complete(self) -> bool:
        """True if agent finished successfully."""
        return self.status == "COMPLETED"


class SuspendInterrupt(Exception):  # noqa: N818 — intentional: interrupt, not error
    """Raised by SyscallProxy to unwind the coroutine stack when HITL is needed."""

    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint


# Rebuild forward refs for SyscallRecord.child_checkpoint
SyscallRecord.model_rebuild()
