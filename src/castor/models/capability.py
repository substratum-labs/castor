"""Capability and Syscall data models."""

from typing import Any, Literal, Optional

from pydantic import BaseModel


class Capability(BaseModel):
    resource_type: str
    max_budget: float
    current_usage: float = 0.0


class SyscallRequest(BaseModel):
    caller_pid: str
    tool_name: str
    arguments: dict[str, Any]


class SyscallResponse(BaseModel):
    status: Literal[
        "SUCCESS",
        "VALIDATION_ERROR",
        "HITL_MODIFIED",
        "HITL_REJECTED",
        "SUSPENDED",
        "INSUFFICIENT_CAPABILITY",
    ]
    result_payload: Optional[Any] = None
    feedback_message: Optional[str] = None
    human_feedback: Optional[str] = None
