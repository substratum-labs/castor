"""Capability and Syscall data models."""

from typing import Any, Literal

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
    result_payload: Any | None = None
    feedback_message: str | None = None
    human_feedback: str | None = None
