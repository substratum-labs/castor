"""Protocol interfaces for Castor kernel subsystem boundaries.

These Protocols define the contracts between components. Today they are
satisfied by in-process concrete classes (Level 0). When Castor moves to
a Rust daemon (castord Level 1+), the same interfaces will be implemented
by IPC proxy classes — callers never change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pydantic import ValidationError

    from castor.gate.registry import ToolMetadata
    from castor.models.capability import Capability, SyscallResponse
    from castor.models.checkpoint import AgentCheckpoint


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

    def create_capabilities(self, specs: dict[str, float]) -> dict[str, Capability]: ...

    def check(
        self,
        capabilities: dict[str, Capability],
        resource_type: str,
        cost: float,
    ) -> bool: ...

    def deduct(
        self,
        capabilities: dict[str, Capability],
        resource_type: str,
        cost: float,
    ) -> None: ...

    def refund(
        self,
        capabilities: dict[str, Capability],
        resource_type: str,
        cost: float,
    ) -> None: ...

    def delegate(
        self,
        parent_caps: dict[str, Capability],
        requested: dict[str, float],
    ) -> dict[str, Capability]: ...

    def reclaim(
        self,
        parent_caps: dict[str, Capability],
        child_caps: dict[str, Capability],
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
    """Interface for context window memory management."""

    @property
    def kernel_tool_names(self) -> set[str]: ...

    async def check_and_evict(
        self, proxy: Any, checkpoint: AgentCheckpoint
    ) -> None: ...


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
