"""The @castor_tool decorator for registering functions as Castor tools."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from pydantic import create_model

from castor.dam.registry import ToolMetadata, ToolRegistry, default_registry


def castor_tool(
    consumes: str,
    cost_per_use: float = 1.0,
    requires_hitl: bool = False,
    destructive: bool = False,
    registry: ToolRegistry | None = None,
    timeout_seconds: float | None = None,
) -> Callable:
    """Register a Python function as a Castor tool.

    Auto-generates a Pydantic input schema from the function's
    type hints and registers the tool in the given registry.
    """
    target_registry = registry or default_registry

    def decorator(func: Callable) -> Callable:
        tool_name = func.__name__
        input_schema = _generate_schema(func)
        is_async = asyncio.iscoroutinefunction(func)

        metadata = ToolMetadata(
            tool_name=tool_name,
            consumes=consumes,
            cost_per_use=cost_per_use,
            requires_hitl=requires_hitl,
            destructive=destructive,
            input_schema=input_schema,
            func=func,
            is_async=is_async,
            timeout_seconds=timeout_seconds,
        )
        target_registry.register(metadata)

        # Attach metadata to the function for introspection
        func._castor_metadata = metadata  # type: ignore[attr-defined]
        return func

    return decorator


def _generate_schema(func: Callable) -> dict[str, Any]:
    """Generate a JSON Schema dict from a function's type hints.

    Uses inspect.signature to extract parameters and pydantic.create_model
    to build a schema. Parameters without defaults become required fields.
    """
    sig = inspect.signature(func)
    annotations = getattr(func, "__annotations__", None) or {}
    hints = {k: v for k, v in annotations.items() if k != "return"}

    field_definitions: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        annotation = hints.get(param_name, Any)

        if param.default is inspect.Parameter.empty:
            # Required field: (type, ...)
            field_definitions[param_name] = (annotation, ...)
        else:
            # Optional field: (type, default)
            field_definitions[param_name] = (annotation, param.default)

    model_name = f"{func.__name__}_InputModel"
    input_model = create_model(model_name, **field_definitions)  # type: ignore[call-overload]
    return input_model.model_json_schema()
