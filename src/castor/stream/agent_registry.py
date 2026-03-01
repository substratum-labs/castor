"""Agent function registry: maps agent names to async callables."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class AgentNotFoundError(Exception):
    """Raised when an agent function name is not found in the registry."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        super().__init__(f"Agent function not found: {agent_name!r}")


# Type alias for agent functions: async def name(proxy) -> Any
AgentFn = Callable[..., Awaitable[Any]]


class AgentRegistry:
    """Registry mapping agent names to async agent functions.

    Mirrors the ToolRegistry pattern from castor.dam.registry.
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentFn] = {}

    def register(self, name: str, fn: AgentFn) -> None:
        """Register an agent function by name."""
        self._agents[name] = fn

    def get(self, name: str) -> AgentFn:
        """Look up an agent function. Raises AgentNotFoundError."""
        if name not in self._agents:
            raise AgentNotFoundError(name)
        return self._agents[name]

    def has_agent(self, name: str) -> bool:
        """Check if an agent is registered."""
        return name in self._agents

    def list_agents(self) -> list[str]:
        """Return sorted list of registered agent names."""
        return sorted(self._agents.keys())


def castor_agent(
    name: str | None = None,
    *,
    registry: AgentRegistry,
) -> Callable:
    """Decorator to register an async function as a Castor agent.

    Usage::

        @castor_agent(name="researcher", registry=my_registry)
        async def researcher_agent(proxy: SyscallProxy) -> str:
            ...
    """

    def decorator(fn: AgentFn) -> AgentFn:
        agent_name = name or fn.__name__
        registry.register(agent_name, fn)
        return fn

    return decorator
