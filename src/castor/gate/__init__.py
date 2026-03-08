"""Castor Gate: tool registry, validation, and execution."""

from castor.gate.decorator import castor_tool
from castor.gate.registry import (
    ToolMetadata,
    ToolNotFoundError,
    ToolRegistry,
    default_registry,
)
from castor.gate.validator import SyscallGate

__all__ = [
    "SyscallGate",
    "ToolMetadata",
    "ToolNotFoundError",
    "ToolRegistry",
    "castor_tool",
    "default_registry",
]
