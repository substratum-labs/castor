"""MnemosLLMSyscall — Castor LLM tool wrapper backed by Mnemos."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from castor.gate.registry import ToolMetadata, ToolRegistry
from castor.mnemos.lifecycle import ContextLifecycleManager
from castor.observability import get_logger
from castor.scheduler.proxy import SyscallProxy

if TYPE_CHECKING:
    from mnemos.client import MnemosClient

_logger = get_logger("castor.mnemos")

DEFAULT_TOOL_NAME = "mnemos_inference"


class MnemosLLMSyscall:
    """Wraps a Mnemos client as a Castor LLM tool.

    From Castor's view, calling Mnemos is just another LLM tool — the
    only difference is that the underlying engine is Mnemos rather than
    OpenAI/Anthropic/etc.

    Per-agent context lifecycle is managed automatically: a `ContextHandle`
    is created on first call for a given pid and reused for subsequent
    calls. Use `drop_for(pid)` to release the context when an agent
    completes.

    Parameters
    ----------
    registry:
        The Castor `ToolRegistry` to register the tool in.
    client:
        A connected `MnemosClient` instance.
    model_id:
        Model identifier passed to Mnemos `create_context`.
    max_tokens:
        Maximum context size for Mnemos contexts (default 4096).
    consumes:
        Capability resource type deducted per call.
    cost_per_use:
        Numeric cost deducted from the capability budget per invocation.
    tool_name:
        Override the registered tool name (default `"mnemos_inference"`).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        client: MnemosClient,
        model_id: str,
        max_tokens: int = 4096,
        consumes: str = "api_usd",
        cost_per_use: float = 0.0,
        tool_name: str = DEFAULT_TOOL_NAME,
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._tool_name = tool_name
        self._lifecycle = ContextLifecycleManager(client)

        # Tool function: takes pid + tokens, returns generated tokens.
        # We capture self via closure so the function is registry-friendly.
        async def _execute(
            pid: str,
            tokens: list[int],
            max_new_tokens: int = 64,
            priority: int = 0,
            resume_token_b64: str | None = None,
        ) -> dict[str, Any]:
            return await self._do_execute(
                pid=pid,
                tokens=tokens,
                max_new_tokens=max_new_tokens,
                priority=priority,
                resume_token_b64=resume_token_b64,
            )

        # Schema kept minimal for M2 — Pydantic input_schema can stay empty
        # since Castor's gate validates by introspection if needed.
        metadata = ToolMetadata(
            tool_name=tool_name,
            consumes=consumes,
            cost_per_use=cost_per_use,
            requires_hitl=False,
            destructive=False,
            input_schema={},
            func=_execute,
            is_async=True,
        )
        self._metadata = metadata
        registry.register(metadata)

    async def _do_execute(
        self,
        pid: str,
        tokens: list[int],
        max_new_tokens: int,
        priority: int,
        resume_token_b64: str | None,
    ) -> dict[str, Any]:
        """Execute Mnemos inference for one syscall.

        Returns a dict: {tokens: list[int], status: str, resume_token: str | None}
        """
        from mnemos.models.execution import (
            Complete,
            EngineInput,
            ExecHint,
            Failed,
            PartialPreempted,
        )

        handle = await self._lifecycle.get_or_create(
            pid=pid, model_id=self._model_id, max_tokens=self._max_tokens
        )

        resume_token: bytes | None = None
        if resume_token_b64:
            resume_token = base64.b64decode(resume_token_b64)

        accumulated: list[int] = []
        final_status = "incomplete"
        final_resume_token: str | None = None
        failure_info: dict[str, Any] | None = None

        async for result in self._client.execute(
            handle,
            EngineInput(tokens=tokens),
            ExecHint(
                priority=priority,
                max_new_tokens=max_new_tokens,
                resume_token=resume_token,
            ),
        ):
            accumulated.extend(result.tokens)
            status = result.completion_status
            if isinstance(status, Complete):
                final_status = "complete"
            elif isinstance(status, PartialPreempted):
                final_status = "partial_preempted"
                final_resume_token = base64.b64encode(status.resume_token).decode()
            elif isinstance(status, Failed):
                final_status = "failed"
                failure_info = {
                    "reason": status.reason.value,
                    "retryable": status.retryable,
                    "context_valid": status.context_valid,
                }

        response: dict[str, Any] = {
            "tokens": accumulated,
            "status": final_status,
        }
        if final_resume_token is not None:
            response["resume_token"] = final_resume_token
        if failure_info is not None:
            response["failure"] = failure_info
        return response

    async def infer(
        self,
        proxy: SyscallProxy,
        tokens: list[int],
        max_new_tokens: int = 64,
        priority: int = 0,
        resume_token: str | None = None,
    ) -> dict[str, Any]:
        """Issue a Mnemos inference call through the Castor proxy.

        The agent's pid is taken from `proxy.checkpoint.pid` and used as
        the context lifecycle key. Subsequent calls from the same agent
        reuse the same Mnemos context.
        """
        return await proxy.syscall(
            self._tool_name,
            {
                "pid": proxy.checkpoint.pid,
                "tokens": tokens,
                "max_new_tokens": max_new_tokens,
                "priority": priority,
                "resume_token_b64": resume_token,
            },
        )

    async def drop_for(self, pid: str) -> None:
        """Drop the Mnemos context for this agent (call on completion)."""
        await self._lifecycle.drop(pid)

    async def drop_all(self) -> None:
        """Drop all Mnemos contexts managed by this syscall."""
        await self._lifecycle.drop_all()

    async def pin_for(self, pid: str) -> None:
        """Pin the Mnemos context for this agent (e.g., during HITL wait).

        No-op if there's no Mnemos context registered for this pid yet.
        """
        handle = self._lifecycle.get(pid)
        if handle is not None:
            await self._client.hint_pin(handle)

    async def unpin_for(self, pid: str) -> None:
        """Unpin the Mnemos context for this agent (e.g., on HITL resume).

        No-op if there's no Mnemos context registered for this pid.
        """
        handle = self._lifecycle.get(pid)
        if handle is not None:
            await self._client.hint_unpin(handle)

    @property
    def lifecycle(self) -> ContextLifecycleManager:
        return self._lifecycle
