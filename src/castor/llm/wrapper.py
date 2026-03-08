"""LLM syscall wrappers: route LLM inference through the SyscallProxy.

LLM API calls are non-deterministic — the same prompt can return different text
on every invocation.  If an agent calls an LLM client directly (bypassing the
proxy), the response is never logged in the ``syscall_log``.  On resume/replay
the live call re-executes, produces different text, and the agent issues a
different syscall sequence, triggering ``ReplayDivergenceError``.

This module provides two helpers:

* ``LLMSyscall`` — wraps a ``call_fn`` that returns a complete string.
* ``StreamingLLMSyscall`` — wraps a ``stream_fn`` (async generator yielding
  chunks).  Each chunk iteration is an ``await`` point, enabling true
  token-level preemption via ``asyncio.Task.cancel()``.  On cancellation,
  accumulated partial text is saved to ``checkpoint.partial_work``.

Usage (non-streaming)::

    from castor.llm import LLMSyscall

    async def call_openai(model: str, prompt: str) -> str:
        resp = await openai_client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content

    llm = LLMSyscall(registry, call_fn=call_openai, consumes="api_usd",
                      cost_per_use=0.03)

    async def my_agent(proxy):
        answer = await llm.infer(proxy, model="gpt-4", prompt="Summarise X")

Usage (streaming — token-level preemption)::

    from castor.llm import StreamingLLMSyscall

    async def stream_openai(model: str, prompt: str):
        stream = await openai_client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    llm = StreamingLLMSyscall(registry, stream_fn=stream_openai,
                               consumes="api_usd", cost_per_use=0.03)

    async def my_agent(proxy):
        answer = await llm.infer(proxy, model="gpt-4", prompt="Summarise X")
        # On preemption mid-stream: checkpoint.partial_work has accumulated text
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import AsyncIterator, Callable
from typing import Any

from castor.gate.registry import ToolMetadata, ToolRegistry
from castor.observability import get_logger, get_meter
from castor.stream.proxy import SyscallProxy

_logger = get_logger("castor.llm")

# Default tool name used when registering the LLM inference syscall.
DEFAULT_TOOL_NAME = "llm_inference"
DEFAULT_STREAMING_TOOL_NAME = "llm_inference_streaming"

# ── ContextVars for per-task streaming state ──
# Each asyncio.Task gets its own copy, so concurrent agents sharing one
# StreamingLLMSyscall instance won't collide.
_streaming_partial: contextvars.ContextVar[str] = contextvars.ContextVar(
    "castor_streaming_partial", default=""
)
_streaming_token_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "castor_streaming_token_count", default=0
)

_meter = get_meter("castor.llm")
_streaming_tokens_counter = _meter.create_counter("castor_llm_tokens_streamed")


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
        registry: ToolRegistry | None = None,
        call_fn: Callable[..., Any] | None = None,
        consumes: str = "api_usd",
        cost_per_use: float = 1.0,
        tool_name: str = DEFAULT_TOOL_NAME,
    ) -> None:
        if call_fn is None:
            raise TypeError("call_fn is required")
        self._tool_name = tool_name
        self._call_fn = call_fn

        # Build the Pydantic input schema by introspection — reuse the same
        # helper the @castor_tool decorator uses internally.  Fall back to an
        # empty schema for callables that lack annotations (e.g. mocks).
        from castor.gate.decorator import _generate_schema

        try:
            schema = _generate_schema(call_fn)
        except (AttributeError, TypeError):
            fn_name = getattr(call_fn, "__name__", repr(call_fn))
            _logger.warning(
                "llm_schema_fallback",
                extra={"tool": tool_name, "call_fn": fn_name},
            )
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
        self._metadata = metadata
        if registry is not None:
            registry.register(metadata)

    async def infer(self, proxy: SyscallProxy, **kwargs: Any) -> Any:
        """Issue an LLM inference call through the proxy.

        Keyword arguments are forwarded as the syscall ``arguments`` dict.
        During replay the cached response is returned without calling the
        LLM provider.
        """
        return await proxy.syscall(self._tool_name, kwargs)


class StreamingLLMSyscall:
    """Wraps an async-generator LLM streaming client as a Castor syscall.

    Unlike ``LLMSyscall`` (which accepts a ``call_fn`` returning a complete
    string), this accepts a ``stream_fn`` — an async generator that yields
    string chunks.  Each chunk iteration is an ``await`` point, enabling
    true **token-level preemption** via ``asyncio.Task.cancel()``.

    On ``CancelledError``, accumulated text is saved to
    ``checkpoint.partial_work`` so the agent can inspect it on resume.

    Parameters
    ----------
    registry:
        The ``ToolRegistry`` to register the tool in.
    stream_fn:
        An async generator ``async def(model, prompt, ...) -> AsyncIterator[str]``
        that yields string chunks from the LLM API.
    consumes:
        Capability resource type deducted per call (e.g. ``"api_usd"``).
    cost_per_use:
        Flat cost deducted before execution (same as ``LLMSyscall``).
    cost_per_token:
        Optional per-token cost.  When set and a call is cancelled mid-stream,
        only the actual tokens consumed are charged (via ``proxy.charge_partial``).
    tool_name:
        Override the registered tool name (default ``"llm_inference_streaming"``).
    on_chunk:
        Synchronous callback ``(chunk: str, accumulated: str) -> None`` fired
        after each chunk.  Keep this fast — it runs on the event loop.
    on_chunk_async:
        Async callback ``(chunk: str, accumulated: str) -> None`` fired after
        each chunk.  May perform I/O (e.g. content safety check).
    """

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        stream_fn: Callable[..., AsyncIterator[str]] | None = None,
        consumes: str = "api_usd",
        cost_per_use: float = 1.0,
        cost_per_token: float | None = None,
        tool_name: str = DEFAULT_STREAMING_TOOL_NAME,
        on_chunk: Callable[[str, str], None] | None = None,
        on_chunk_async: Callable[[str, str], Any] | None = None,
    ) -> None:
        if stream_fn is None:
            raise TypeError("stream_fn is required")
        self._tool_name = tool_name
        self._stream_fn = stream_fn
        self._on_chunk = on_chunk
        self._on_chunk_async = on_chunk_async
        self._consumes = consumes
        self._cost_per_use = cost_per_use
        self._cost_per_token = cost_per_token

        # Build the accumulating wrapper that will be registered as the tool.
        # It iterates the async generator, accumulates chunks into a full
        # response string, and updates ContextVars for partial-work capture.
        _on_chunk_sync = on_chunk
        _on_chunk_awaitable = on_chunk_async
        _tool = tool_name

        @functools.wraps(stream_fn)
        async def _accumulate(**kwargs: Any) -> str:
            accumulated = ""
            token_count = 0
            async for chunk in stream_fn(**kwargs):
                accumulated += chunk
                token_count += 1
                # Update ContextVars so CancelledError handler can read them.
                # set() is synchronous — cannot be interrupted.
                _streaming_partial.set(accumulated)
                _streaming_token_count.set(token_count)
                # Observability
                _streaming_tokens_counter.add(1, {"tool": _tool})
                # Callbacks
                if _on_chunk_sync is not None:
                    _on_chunk_sync(chunk, accumulated)
                if _on_chunk_awaitable is not None:
                    await _on_chunk_awaitable(chunk, accumulated)
            return accumulated

        # Introspect stream_fn for Pydantic input schema (same as LLMSyscall).
        from castor.gate.decorator import _generate_schema

        try:
            schema = _generate_schema(stream_fn)
        except (AttributeError, TypeError):
            fn_name = getattr(stream_fn, "__name__", repr(stream_fn))
            _logger.warning(
                "llm_schema_fallback",
                extra={"tool": tool_name, "stream_fn": fn_name},
            )
            schema = {}

        metadata = ToolMetadata(
            tool_name=tool_name,
            consumes=consumes,
            cost_per_use=cost_per_use,
            cost_per_token=cost_per_token,
            requires_hitl=False,
            destructive=False,
            input_schema=schema,
            func=_accumulate,
            is_async=True,
        )
        self._metadata = metadata
        if registry is not None:
            registry.register(metadata)

    async def infer(self, proxy: SyscallProxy, **kwargs: Any) -> Any:
        """Issue a streaming LLM inference call through the proxy.

        Keyword arguments are forwarded as the syscall ``arguments`` dict.
        During replay the cached response is returned without calling the
        LLM provider (identical to ``LLMSyscall``).

        On ``CancelledError``: saves accumulated partial text to
        ``checkpoint.partial_work`` and charges proportional budget
        (if ``cost_per_token`` was configured).
        """
        partial_token = _streaming_partial.set("")
        count_token = _streaming_token_count.set(0)
        try:
            return await proxy.syscall(self._tool_name, kwargs)
        except asyncio.CancelledError:
            # Save partial work before re-raising.
            partial = _streaming_partial.get()
            if partial:
                proxy.checkpoint.partial_work = partial
                _logger.info(
                    "streaming_partial_saved",
                    extra={"tool": self._tool_name, "partial_len": len(partial)},
                )
            # Proportional budget: proxy already did a full refund in its
            # BaseException handler.  Re-deduct actual consumption.
            if self._cost_per_token is not None:
                token_count = _streaming_token_count.get()
                if token_count > 0:
                    actual_cost = min(
                        token_count * self._cost_per_token, self._cost_per_use
                    )
                    proxy.charge_partial(self._consumes, actual_cost)
                    _logger.info(
                        "streaming_proportional_charge",
                        extra={
                            "tool": self._tool_name,
                            "tokens": token_count,
                            "actual_cost": actual_cost,
                        },
                    )
            raise
        finally:
            _streaming_partial.reset(partial_token)
            _streaming_token_count.reset(count_token)
