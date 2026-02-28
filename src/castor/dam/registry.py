"""Tool registry and metadata for Castor Dam."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolNotFoundError(Exception):
    """Raised when a tool is not found in the registry."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool not found: {tool_name!r}")


class ToolMetadata(BaseModel):
    """Metadata for a registered tool."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    consumes: str
    cost_per_use: float = 1.0
    requires_hitl: bool = False
    destructive: bool = False
    input_schema: dict[str, Any] = {}
    func: Callable | None = None
    is_async: bool = False


class ToolRegistry:
    """Registry for Castor tools. Stores metadata and provides lookup."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolMetadata] = {}

    def register(self, metadata: ToolMetadata) -> None:
        """Register a tool with its metadata."""
        self._tools[metadata.tool_name] = metadata

    def get(self, tool_name: str) -> ToolMetadata:
        """Look up a tool by name. Raises ToolNotFoundError if not found."""
        if tool_name not in self._tools:
            raise ToolNotFoundError(tool_name)
        return self._tools[tool_name]

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool is registered."""
        return tool_name in self._tools

    def list_tools(self) -> list[str]:
        """Return sorted list of registered tool names."""
        return sorted(self._tools.keys())


# Module-level default registry instance
default_registry = ToolRegistry()
