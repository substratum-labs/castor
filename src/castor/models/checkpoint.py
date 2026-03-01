"""Checkpoint/Replay data models for the Castor Stream scheduler."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from castor.models.capability import Capability


class CastorMessage(BaseModel):
    """A message in the agent's context history with Lodge metadata."""

    role: str
    content: str
    pinned: bool = False
    token_count: int = 0


class SyscallRecord(BaseModel):
    request: dict[str, Any]
    response: Any
    was_hitl: bool = False
    child_checkpoint: AgentCheckpoint | None = None


class AgentCheckpoint(BaseModel):
    pid: str
    parent_pid: str | None = None
    status: Literal["RUNNING", "SUSPENDED_FOR_HITL", "PREEMPTED", "COMPLETED", "FAILED"]
    agent_function_name: str
    capabilities: dict[str, Capability]
    syscall_log: list[SyscallRecord] = []
    pending_hitl: dict[str, Any] | None = None
    context_history: list[CastorMessage | dict[str, Any]] = []

    # Preemption context (informational, not part of deterministic replay)
    preemption_reason: str | None = None
    preemption_payload: dict[str, Any] | None = None
    partial_work: str | None = None


class SuspendInterrupt(Exception):  # noqa: N818 — intentional: interrupt, not error
    """Raised by SyscallProxy to unwind the coroutine stack when HITL is needed."""

    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint


# Rebuild forward refs for SyscallRecord.child_checkpoint
SyscallRecord.model_rebuild()
