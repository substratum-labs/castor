"""Protocol interfaces for Castor kernel subsystem boundaries.

These Protocols define the contracts between components. Today they are
satisfied by in-process concrete classes (Level 0). When Castor moves to
a Rust daemon (castord Level 1+), the same interfaces will be implemented
by IPC proxy classes — callers never change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pydantic import ValidationError

    from castor.gate.registry import ToolMetadata
    from castor.models.budget import Budget, SyscallResponse
    from castor.models.checkpoint import AgentCheckpoint, SyscallRecord


# ---------------------------------------------------------------------------
# Gate — tool validation and execution
# ---------------------------------------------------------------------------


@runtime_checkable
class GateProtocol(Protocol):
    """Interface for tool validation, metadata lookup, and execution."""

    def validate(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def get_tool_meta(self, tool_name: str) -> ToolMetadata: ...

    async def execute(self, tool_name: str, validated_args: dict[str, Any]) -> Any: ...

    def format_validation_error(
        self, tool_name: str, error: ValidationError
    ) -> SyscallResponse: ...

    def has_tool(self, tool_name: str) -> bool: ...

    def list_tools(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Budget — capability budget management
# ---------------------------------------------------------------------------


@runtime_checkable
class BudgetProtocol(Protocol):
    """Interface for capability budget tracking and delegation."""

    def create_budgets(self, specs: dict[str, float]) -> dict[str, Budget]: ...

    def check(
        self,
        capabilities: dict[str, Budget],
        resource_type: str,
        cost: float,
    ) -> bool: ...

    def deduct(
        self,
        capabilities: dict[str, Budget],
        resource_type: str,
        cost: float,
    ) -> None: ...

    def refund(
        self,
        capabilities: dict[str, Budget],
        resource_type: str,
        cost: float,
    ) -> None: ...

    def delegate(
        self,
        parent_budgets: dict[str, Budget],
        requested: dict[str, float],
    ) -> dict[str, Budget]: ...

    def reclaim(
        self,
        parent_budgets: dict[str, Budget],
        child_budgets: dict[str, Budget],
    ) -> None: ...


# ---------------------------------------------------------------------------
# Checkpoint Store — persistence and WAL
# ---------------------------------------------------------------------------


@runtime_checkable
class CheckpointStoreProtocol(Protocol):
    """Interface for checkpoint persistence and write-ahead log."""

    def save(self, checkpoint: AgentCheckpoint) -> None: ...

    def load(self, pid: str) -> AgentCheckpoint: ...

    def delete(self, pid: str) -> None: ...

    def list_pids(self) -> list[str]: ...

    def write_wal(
        self,
        pid: str,
        syscall_index: int,
        tool_name: str,
        arguments: dict[str, Any],
        budget_snapshot: dict[str, float],
    ) -> None: ...

    def complete_wal(self, pid: str, syscall_index: int, result: Any) -> None: ...

    def abandon_wal(self, pid: str, syscall_index: int) -> None: ...


# ---------------------------------------------------------------------------
# MMU — context window memory management
# ---------------------------------------------------------------------------


@runtime_checkable
class MMUProtocol(Protocol):
    """Interface for context window memory management.

    The MMU bridges the MemoryPolicy (application-level) with kernel
    syscalls (execution-truth level). It provides the hard-watermark
    safety net and dispatches evict/recall/pin/store operations through
    the proxy so they appear in the journal.
    """

    @property
    def kernel_tool_names(self) -> set[str]: ...

    async def check_and_evict(
        self, proxy: Any, checkpoint: AgentCheckpoint
    ) -> None: ...

    def pause_auto_evict(self) -> None: ...

    def resume_auto_evict(self) -> None: ...


# ---------------------------------------------------------------------------
# Memory Policy — application-level eviction & recall strategy
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryPolicyProtocol(Protocol):
    """Application-provided memory management strategy.

    The kernel calls these methods but does NOT depend on a specific
    implementation. Tiphys provides a semantic + episodic policy;
    castor-server provides a simple FIFO. The kernel only uses the
    returned decisions to issue mem_evict / mem_recall syscalls.

    This is the "page replacement algorithm" in OS terms — it lives
    above the kernel primitives but its decisions are executed through
    kernel syscalls for replay safety.
    """

    async def should_evict(
        self,
        context_history: list[Any],
        token_budget: int,
    ) -> list[int] | None:
        """Return message indices to evict, or ``None`` if no eviction needed.

        Called by the MMU when the soft watermark is approached. The
        returned indices will be passed to ``mem_evict``. Returning
        ``None`` skips voluntary eviction (the hard watermark may still
        trigger FIFO eviction as a safety net).
        """
        ...

    async def generate_summary(
        self,
        evicted_messages: list[Any],
    ) -> str | None:
        """Optionally summarize evicted messages for context retention.

        If a string is returned, the MMU issues a ``mem_summarize``
        syscall (which goes through the journal and costs budget) to
        produce a summary message that stays in ``context_history``.

        Return ``None`` to skip summarization.
        """
        ...

    async def should_recall(
        self,
        context_history: list[Any],
        current_query: str,
    ) -> str | None:
        """Return a recall query if cold storage should be searched.

        Called before each LLM turn. If a non-``None`` string is
        returned, the MMU issues a ``mem_recall`` syscall to fetch
        relevant messages from cold storage and insert them into
        ``context_history``.
        """
        ...

    async def on_session_end(
        self,
        context_history: list[Any],
        syscall_log: list[Any],
    ) -> None:
        """Hook invoked when a session completes or is terminated.

        Use this for post-session consolidation: episodic lesson
        extraction, knowledge distillation, cache warming, etc.
        This is *not* a syscall — it runs after the journal is sealed.
        """
        ...


# ---------------------------------------------------------------------------
# Cold Storage Backend — evicted / explicit memory persistence
# ---------------------------------------------------------------------------


@runtime_checkable
class ColdStorageProtocol(Protocol):
    """Backend for persisting evicted messages and explicit mem_store data.

    Separate from the agent's application-level memory (e.g. Tiphys's
    ScopedMemoryStore or Evolution Ledger). The two are queried
    through a unified retrieval interface but stored independently
    (Decision 3 → B: separate storage, unified retrieval).

    Namespace by ``agent_id`` (not session_id) so evicted context is
    shared across sessions of the same agent (Tiphys requirement:
    cross-session ColdStorage sharing).
    """

    async def store(
        self,
        agent_id: str,
        messages: list[Any],
        summary: str | None = None,
        source: str = "eviction",
    ) -> None:
        """Persist messages (and optional summary) to cold storage.

        ``source`` distinguishes eviction-driven storage from explicit
        ``mem_store`` calls so retrieval can filter by provenance.
        """
        ...

    async def search(
        self,
        agent_id: str,
        query: str,
        max_results: int = 5,
        source_filter: str | None = None,
    ) -> list[Any]:
        """Retrieve relevant messages from cold storage.

        ``source_filter`` optionally restricts to a specific provenance
        (e.g. ``"eviction"``, ``"explicit"``, ``"episodic"``).
        """
        ...

    async def store_explicit(
        self,
        agent_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store an explicit memory entry (from ``mem_store`` syscall).

        Unlike ``store()`` which receives evicted CastorMessages, this
        stores arbitrary content the agent explicitly wanted to remember.
        """
        ...


# ---------------------------------------------------------------------------
# Agent Registry — agent function lookup
# ---------------------------------------------------------------------------

AgentFn = Callable[..., Awaitable[Any]]


@runtime_checkable
class AgentRegistryProtocol(Protocol):
    """Interface for agent function discovery and lookup."""

    def get(self, name: str) -> AgentFn: ...

    def has_agent(self, name: str) -> bool: ...

    def list_agents(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Journal — syscall event log
# ---------------------------------------------------------------------------


@runtime_checkable
class JournalProtocol(Protocol):
    """Interface for the syscall event journal (append-only log).

    Level 0: wraps ``checkpoint.syscall_log`` (in-memory list).
    Level 1: SQLite WAL.
    Level 2: distributed journal service with ``subscribe()``.
    """

    def append(self, record: SyscallRecord) -> int:
        """Append a record and return its index."""
        ...

    def get(self, index: int) -> SyscallRecord:
        """Get a record by index."""
        ...

    def __len__(self) -> int:
        """Return the number of records."""
        ...

    def scan_from(self, index: int) -> Iterator[tuple[int, SyscallRecord]]:
        """Iterate (index, record) pairs starting from *index*."""
        ...


# ---------------------------------------------------------------------------
# Runner — agent execution scheduling
# ---------------------------------------------------------------------------


@runtime_checkable
class RunnerProtocol(Protocol):
    """Interface for agent execution scheduling.

    The Runner is responsible for:
    - Creating a SyscallProxy and setting the ContextVar bridge
    - Invoking the agent function
    - Handling completion, suspension (HITL), and preemption

    Level 0: ``AgentRunner`` — sequential, non-real-time (digital scenarios).
    Pollux: ``PolluxRunner`` — real-time priority scheduling (embodied scenarios).
    """

    async def run(
        self,
        agent_fn: Callable[..., Awaitable[Any]],
        checkpoint: AgentCheckpoint,
    ) -> AgentCheckpoint:
        """Execute an agent function and return the final checkpoint."""
        ...

    async def run_as_task(
        self,
        agent_fn: Callable[..., Awaitable[Any]],
        checkpoint: AgentCheckpoint,
    ) -> asyncio.Task:
        """Launch an agent as a background asyncio.Task for preemption."""
        ...

    def preempt(self, reason: str, payload: dict[str, Any] | None = None) -> None:
        """Preempt the currently running agent task."""
        ...
