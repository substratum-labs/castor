"""Checkpoint/Replay data models for the Castor Stream scheduler."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

from castor.models.capability import Capability


class SyscallRecord(BaseModel):
    request: dict[str, Any]
    response: Any
    was_hitl: bool = False
    child_checkpoint: Optional[AgentCheckpoint] = None


class AgentCheckpoint(BaseModel):
    pid: str
    parent_pid: Optional[str] = None
    status: Literal["RUNNING", "SUSPENDED_FOR_HITL", "COMPLETED", "FAILED"]
    agent_function_name: str
    capabilities: dict[str, Capability]
    syscall_log: list[SyscallRecord] = []
    pending_hitl: Optional[dict[str, Any]] = None
    context_history: list[dict[str, Any]] = []


class SuspendInterrupt(Exception):
    """Raised by SyscallProxy to unwind the coroutine stack when HITL is needed."""

    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint


# Rebuild forward refs for SyscallRecord.child_checkpoint
SyscallRecord.model_rebuild()
