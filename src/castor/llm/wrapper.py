"""LLMSyscall: route LLM inference through the SyscallProxy for replay safety.

LLM API calls are non-deterministic — the same prompt can return different text
on every invocation.  If an agent calls an LLM client directly (bypassing the
proxy), the response is never logged in the ``syscall_log``.  On resume/replay
the live call re-executes, produces different text, and the agent issues a
different syscall sequence, triggering ``ReplayDivergenceError``.

This module provides ``LLMSyscall``, a thin helper that registers a
``@castor_tool`` backed by a user-supplied async callable and exposes a
convenience ``infer()`` method that delegates to ``proxy.syscall()``.

Usage::

    from castor.llm import LLMSyscall

    # 1. Define your LLM client callback
    async def call_openai(model: str, prompt: str) -> str:
        resp = await openai_client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content

    # 2. Create the syscall wrapper (registers the tool in the registry)
    llm = LLMSyscall(registry, call_fn=call_openai, consumes="api_usd",
                      cost_per_use=0.03)

    # 3. Inside your agent function, call via the proxy
    async def my_agent(proxy):
        answer = await llm.infer(proxy, model="gpt-4", prompt="Summarise X")
        ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from castor.dam.registry import ToolMetadata, ToolRegistry
from castor.stream.proxy import SyscallProxy

# Default tool name used when registering the LLM inference syscall.
DEFAULT_TOOL_NAME = "llm_inference"


class LLMSyscall:
    """Wraps an arbitrary async LLM client function as a Castor syscall.

    Parameters
    ----------
    registry:
        The ``ToolRegistry`` to register the tool in.
    call_fn:
        An ``async def(model: str, prompt: str) -> str`` (or equivalent)
        that performs the actual LLM API call.
    consumes:
        Capability resource type deducted per call (e.g. ``"api_usd"``).
    cost_per_use:
        Numeric cost deducted from the capability budget per invocation.
    tool_name:
        Override the registered tool name (default ``"llm_inference"``).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        call_fn: Callable[..., Any],
        consumes: str = "api_usd",
        cost_per_use: float = 1.0,
        tool_name: str = DEFAULT_TOOL_NAME,
    ) -> None:
        self._tool_name = tool_name
        self._call_fn = call_fn

        # Build the Pydantic input schema by introspection — reuse the same
        # helper the @castor_tool decorator uses internally.  Fall back to an
        # empty schema for callables that lack annotations (e.g. mocks).
        from castor.dam.decorator import _generate_schema

        try:
            schema = _generate_schema(call_fn)
        except (AttributeError, TypeError):
            schema = {}

        metadata = ToolMetadata(
            tool_name=tool_name,
            consumes=consumes,
            cost_per_use=cost_per_use,
            requires_hitl=False,
            destructive=False,
            input_schema=schema,
            func=call_fn,
            is_async=True,
        )
        registry.register(metadata)

    async def infer(self, proxy: SyscallProxy, **kwargs: Any) -> Any:
        """Issue an LLM inference call through the proxy.

        Keyword arguments are forwarded as the syscall ``arguments`` dict.
        During replay the cached response is returned without calling the
        LLM provider.
        """
        return await proxy.syscall(self._tool_name, kwargs)
