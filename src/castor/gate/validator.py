"""SyscallGate: validation and execution engine for registered tools."""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import ValidationError, create_model

from castor.gate.registry import ToolMetadata, ToolRegistry, prepare_execution_arguments
from castor.models.budget import SyscallResponse


class SyscallGate:
    """Validates tool arguments and executes registered tools.

    Combines the ToolRegistry with Pydantic validation and tool execution.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self._input_models: dict[str, type] = {}

    def get_tool_meta(self, tool_name: str) -> ToolMetadata:
        """Look up tool metadata. Raises ToolNotFoundError if missing."""
        return self.registry.get(tool_name)

    def validate(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate arguments against the tool's schema.

        Returns validated arguments (with defaults applied).
        Passes through unchanged if the tool has no schema (e.g. LLM tools).
        Raises pydantic.ValidationError on invalid input.
        """
        meta = self.registry.get(tool_name)
        if not meta.input_schema:
            return arguments  # No schema -> pass through (e.g. LLM wrappers)
        model_cls = self._get_or_build_model(tool_name)
        instance = model_cls(**arguments)
        return instance.model_dump()

    async def execute(self, tool_name: str, validated_args: dict[str, Any]) -> Any:
        """Execute a tool with pre-validated arguments."""
        meta = self.registry.get(tool_name)
        if meta.func is None:
            raise RuntimeError(f"Tool {tool_name!r} has no callable function")

        if meta.is_async:
            return await meta.func(**prepare_execution_arguments(meta, validated_args))
        else:
            return meta.func(**prepare_execution_arguments(meta, validated_args))

    def format_validation_error(
        self, tool_name: str, error: ValidationError
    ) -> SyscallResponse:
        """Convert a ValidationError into a natural language SyscallResponse."""
        error_details = []
        for err in error.errors():
            field = " -> ".join(str(loc) for loc in err["loc"])
            msg = err["msg"]
            error_details.append(f"  - {field}: {msg}")

        feedback = (
            f"Validation failed for tool '{tool_name}':\n"
            + "\n".join(error_details)
            + "\nPlease fix the arguments and try again."
        )

        return SyscallResponse(
            status="VALIDATION_ERROR",
            feedback_message=feedback,
        )

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool is registered."""
        return self.registry.has_tool(tool_name)

    def list_tools(self) -> list[str]:
        """Return sorted list of registered tool names."""
        return self.registry.list_tools()

    def _get_or_build_model(self, tool_name: str) -> type:
        """Get or lazily build a Pydantic model for a tool's input schema."""
        if tool_name not in self._input_models:
            meta = self.registry.get(tool_name)
            if meta.func is None:
                raise RuntimeError(f"Tool {tool_name!r} has no callable function")
            self._input_models[tool_name] = _build_input_model(meta)
        return self._input_models[tool_name]


def _build_input_model(meta: ToolMetadata) -> type:
    """Build a Pydantic model from a tool function's signature."""
    func = meta.func
    sig = inspect.signature(func)
    annotations = getattr(func, "__annotations__", None) or {}
    hints = {k: v for k, v in annotations.items() if k != "return"}

    field_definitions: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        annotation = hints.get(param_name, Any)
        if param.default is inspect.Parameter.empty:
            field_definitions[param_name] = (annotation, ...)
        else:
            field_definitions[param_name] = (annotation, param.default)

    return create_model(f"{meta.tool_name}_InputModel", **field_definitions)  # type: ignore[call-overload]
