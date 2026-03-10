"""Spawn primitives: spawn, join."""

from __future__ import annotations

from typing import Any

from castor.lib._context import get_proxy


async def spawn(
    agent_name: str, *, capabilities: dict[str, float] | None = None
) -> str:
    """Spawn a child agent asynchronously, return a join handle."""
    return await get_proxy().spawn(agent_name, capabilities=capabilities)


async def join(handle: str) -> Any:
    """Wait for a spawned child agent to complete and return its result."""
    return await get_proxy().join(handle)
