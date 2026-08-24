"""Structured, read-only queries over an agent's own syscall journal."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from castor.models.checkpoint import SyscallPurpose


class IntrospectionQuery(BaseModel):
    """Base type for the discriminated v1 introspection query set."""


class FindSyscallQuery(IntrospectionQuery):
    type: Literal["find_syscall"] = "find_syscall"
    purpose: SyscallPurpose | None = None
    syscall_name: str | None = None
    step_range: tuple[int, int] | None = None
    cost_min: float | None = None
    duration_ms_min: float | None = None
    limit: int = 50


class GetSyscallQuery(IntrospectionQuery):
    type: Literal["get_syscall"] = "get_syscall"
    target: int | str
    include_full_output: bool = False


class GetReasoningChainQuery(IntrospectionQuery):
    type: Literal["get_reasoning_chain"] = "get_reasoning_chain"
    target_step: int
    max_depth: int = 10


class SummarizeQuery(IntrospectionQuery):
    type: Literal["summarize"] = "summarize"
    step_range: tuple[int, int] | None = None
    group_by: Literal["purpose", "syscall_name", "none"] = "none"


class FindDecisionsQuery(IntrospectionQuery):
    type: Literal["find_decisions"] = "find_decisions"
    output_pattern: str
    step_range: tuple[int, int] | None = None
    limit: int = 20


class SyscallSnapshot(BaseModel):
    invocation_id: str
    syscall_index: int
    name: str
    purpose: SyscallPurpose
    args_summary: str
    output_summary: str
    output_digest: str
    cost: float
    duration_ms: float
    timestamp: float
    raised_exception: str | None


class FindSyscallResult(BaseModel):
    type: Literal["find_syscall"] = "find_syscall"
    matches: list[SyscallSnapshot]
    truncated: bool


class GetSyscallResult(BaseModel):
    type: Literal["get_syscall"] = "get_syscall"
    snapshot: SyscallSnapshot


class ReasoningChainResult(BaseModel):
    type: Literal["get_reasoning_chain"] = "get_reasoning_chain"
    target_step: int
    chain: list[SyscallSnapshot]
    truncated_at_max_depth: bool


class SummarizeResult(BaseModel):
    type: Literal["summarize"] = "summarize"
    total_syscalls: int
    total_cost: float
    total_duration_ms: float
    error_count: int
    by_group: dict[str, dict[str, float]] | None


class FindDecisionsResult(BaseModel):
    type: Literal["find_decisions"] = "find_decisions"
    matches: list[SyscallSnapshot]
    truncated: bool


class PartialResult(BaseModel):
    type: Literal["partial"] = "partial"
    partial_payload: Any
    timeout_at_step: int


Payload = (
    FindSyscallResult
    | GetSyscallResult
    | ReasoningChainResult
    | SummarizeResult
    | FindDecisionsResult
    | PartialResult
)


class IntrospectionResult(BaseModel):
    query_type: str
    payload: Payload
    duration_ms: float


class IntrospectionError(Exception):
    """Base class for introspection failures."""


class IntrospectionTargetNotFoundError(IntrospectionError):
    """The requested journal index or invocation id does not exist."""


class IntrospectionScopeError(IntrospectionError):
    """Reserved for cross-session access once capability tokens exist."""
