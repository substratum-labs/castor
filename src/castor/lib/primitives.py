"""Core primitives: tool, chat, budget, try_tool."""

from __future__ import annotations

from typing import Any

from castor.lib._context import get_proxy


async def tool(name: str, /, **kwargs: Any) -> Any:
    """Call a registered tool by name."""
    return await get_proxy().syscall(name, **kwargs)


async def chat(
    prompt: str,
    *,
    system: str = "",
    tool_name: str = "llm_inference",
) -> str:
    """Call an LLM tool."""
    return await get_proxy().syscall(tool_name, prompt=prompt, system=system)


def budget(resource: str) -> float:
    """Return remaining budget for a resource type."""
    return get_proxy().budget(resource)


async def try_tool(name: str, /, **kwargs: Any) -> Any:
    """Call a tool — semantic alias communicating that failure is expected."""
    return await get_proxy().syscall(name, **kwargs)
