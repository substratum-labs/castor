"""MnemosCastor — Castor wrapper that integrates Mnemos lifecycle hooks.

Composes a `Castor` instance with a `MnemosLLMSyscall` to automatically
manage Mnemos context lifetimes around agent execution:

- On agent ``COMPLETED`` or ``FAILED`` → drop the Mnemos context (free KV).
- On agent ``SUSPENDED_FOR_HITL`` → pin the Mnemos context so Mnemos won't
  evict its KV while the human reviews.
- On ``approve`` / ``reject`` / ``modify`` → unpin before forwarding.

Castor core is **not** modified by this wrapper — it composes with any
Castor instance and is fully opt-in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from castor.core import Castor
from castor.mnemos.wrapper import MnemosLLMSyscall
from castor.models.checkpoint import AgentCheckpoint


class MnemosCastor:
    """Castor wrapped with automatic Mnemos context lifecycle management.

    Usage::

        kernel = Castor(tools=[...], ...)
        syscall = MnemosLLMSyscall(kernel.gate.registry, client, model_id="...")
        mkernel = MnemosCastor(kernel, syscall)

        cp = await mkernel.run(my_agent, budgets={"api_usd": 10.0})
        # Context auto-dropped on completion — no manual drop_for() needed.
    """

    def __init__(self, kernel: Castor, syscall: MnemosLLMSyscall) -> None:
        self._kernel = kernel
        self._syscall = syscall

    @property
    def kernel(self) -> Castor:
        return self._kernel

    @property
    def syscall(self) -> MnemosLLMSyscall:
        return self._syscall

    async def run(self, agent_fn: Callable[..., Any], **kwargs: Any) -> AgentCheckpoint:
        cp = await self._kernel.run(agent_fn, **kwargs)
        await self._handle_post_run(cp)
        return cp

    async def run_until_complete(
        self,
        agent_fn: Callable[..., Any],
        *,
        on_hitl: Callable[[AgentCheckpoint], Awaitable[tuple[str, str | None]]],
        **kwargs: Any,
    ) -> AgentCheckpoint:
        """Run an agent with auto-HITL handling, pinning context during each wait.

        Wraps the user's ``on_hitl`` callback: pins the Mnemos context before
        the callback runs, unpins after. The wrapped callback is still
        responsible for returning ``(decision, feedback)``.
        """

        async def wrapped_on_hitl(
            cp: AgentCheckpoint,
        ) -> tuple[str, str | None]:
            await self._syscall.pin_for(cp.pid)
            try:
                return await on_hitl(cp)
            finally:
                await self._syscall.unpin_for(cp.pid)

        cp = await self._kernel.run_until_complete(
            agent_fn, on_hitl=wrapped_on_hitl, **kwargs
        )
        await self._handle_post_run(cp)
        return cp

    async def approve(self, checkpoint: AgentCheckpoint | str) -> None:
        pid = self._pid_of(checkpoint)
        if pid is not None:
            await self._syscall.unpin_for(pid)
        await self._kernel.approve(checkpoint)

    async def reject(self, checkpoint: AgentCheckpoint | str, reason: str) -> None:
        pid = self._pid_of(checkpoint)
        if pid is not None:
            await self._syscall.unpin_for(pid)
        # Castor.reject is sync
        self._kernel.reject(checkpoint, reason)

    async def modify(self, checkpoint: AgentCheckpoint | str, feedback: str) -> None:
        pid = self._pid_of(checkpoint)
        if pid is not None:
            await self._syscall.unpin_for(pid)
        # Castor.modify is sync
        self._kernel.modify(checkpoint, feedback)

    async def _handle_post_run(self, cp: AgentCheckpoint) -> None:
        if cp.status in ("COMPLETED", "FAILED"):
            await self._syscall.drop_for(cp.pid)
        elif cp.status == "SUSPENDED_FOR_HITL":
            await self._syscall.pin_for(cp.pid)

    @staticmethod
    def _pid_of(checkpoint: AgentCheckpoint | str) -> str | None:
        if isinstance(checkpoint, str):
            return checkpoint
        if hasattr(checkpoint, "pid"):
            return checkpoint.pid
        return None
