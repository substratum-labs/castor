"""Castor Dam: tool registry, validation, and execution."""

from castor.dam.decorator import castor_tool
from castor.dam.registry import (
    ToolMetadata,
    ToolNotFoundError,
    ToolRegistry,
    default_registry,
)
from castor.dam.validator import CastorDam

__all__ = [
    "CastorDam",
    "ToolMetadata",
    "ToolNotFoundError",
    "ToolRegistry",
    "castor_tool",
    "default_registry",
]
