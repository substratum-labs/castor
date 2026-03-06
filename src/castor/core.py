"""Castor: the unified kernel facade."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from castor.capability.manager import CapabilityManager
from castor.dam.registry import ToolRegistry, default_registry
from castor.dam.validator import CastorDam
from castor.models.checkpoint import AgentCheckpoint
from castor.stream.hitl import HITLHandler
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner


class Castor:
    """Unified kernel facade — assembles all subsystems behind a single object.

    Usage::

        kernel = Castor()
        cp = await kernel.run(my_agent, budgets={"api": 50.0})
    """

    def __init__(
        self,
        *,
        tools: list[Callable] | None = None,
        lodge: Any | None = None,
        agent_registry: Any | None = None,
        store: str | Any | None = None,
        dam: CastorDam | None = None,
        capability_manager: CapabilityManager | None = None,
        structured_results: bool = False,
    ) -> None:
        # ── Dam (tool validation + execution) ──
        if dam is not None:
            self._dam = dam
        elif tools is not None:
            from castor.llm.wrapper import LLMSyscall, StreamingLLMSyscall

            registry = ToolRegistry()
            for item in tools:
                if isinstance(item, (LLMSyscall, StreamingLLMSyscall)):
                    registry.register(item._metadata)
                else:
                    meta = getattr(item, "_castor_metadata", None)
                    if meta is None:
                        raise TypeError(
                            f"{item!r} is not a @castor_tool or LLMSyscall instance"
                        )
                    registry.register(meta)
            self._dam = CastorDam(registry)
        else:
            self._dam = CastorDam(default_registry)

        # ── Capability Manager ──
        self._cap_mgr = capability_manager or CapabilityManager()

        # ── Optional subsystems ──
        self._lodge = lodge
        if agent_registry is not None:
            self._agent_registry = agent_registry
        else:
            from castor.stream.agent_registry import default_agent_registry

            if default_agent_registry.list_agents():
                self._agent_registry = default_agent_registry
            else:
                self._agent_registry = None
        self._structured_results = structured_results
        self._hitl = HITLHandler()

        # ── Persistence ──
        self._store = None
        if store is not None:
            from castor.stream.persistence import CheckpointStore

            if isinstance(store, str):
                self._store = CheckpointStore(store)
            else:
                self._store = store

    async def run(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        *,
        budgets: dict[str, float] | None = None,
        checkpoint: AgentCheckpoint | None = None,
        pid: str | None = None,
    ) -> AgentCheckpoint:
        """Run an agent function.

        Args:
            agent_fn: The agent coroutine ``async def agent(proxy) -> result``.
            budgets: Resource budgets like ``{"api": 50.0}``.
                     Not provided = unlimited (no budget enforcement).
            checkpoint: Pass an existing checkpoint to resume (e.g. after HITL).
            pid: Custom process ID. Auto-generated if not provided.
        """
        if checkpoint is None:
            if budgets is not None:
                caps = self._cap_mgr.create_capabilities(budgets)
            else:
                caps = {}
            if pid is None:
                pid = f"{agent_fn.__name__}-{uuid.uuid4().hex[:8]}"
            checkpoint = AgentCheckpoint(
                pid=pid,
                status="RUNNING",
                agent_function_name=agent_fn.__name__,
                capabilities=caps,
            )

        runner = AgentRunner(
            self._dam,
            self._cap_mgr,
            lodge=self._lodge,
            agent_registry=self._agent_registry,
            structured_results=self._structured_results,
        )
        return await runner.run(agent_fn, checkpoint)

    async def approve(self, checkpoint: AgentCheckpoint) -> None:
        """Approve a pending HITL syscall."""
        if self._hitl.is_child_hitl(checkpoint):
            if self._agent_registry is None:
                raise RuntimeError(
                    "Child HITL approval requires an agent_registry on Castor"
                )
            await self._hitl.approve_child_hitl(
                checkpoint,
                self._dam,
                self._cap_mgr,
                self._agent_registry,
                lodge=self._lodge,
            )
        else:
            await self._hitl.approve(checkpoint, self._dam, self._cap_mgr)

    def reject(self, checkpoint: AgentCheckpoint, reason: str) -> None:
        """Reject a pending HITL syscall with feedback."""
        if self._hitl.is_child_hitl(checkpoint):
            raise NotImplementedError(
                "Child HITL rejection requires runtime — use HITLHandler directly"
            )
        self._hitl.reject(checkpoint, reason)

    def modify(self, checkpoint: AgentCheckpoint, feedback: str) -> None:
        """Approve with modification — log feedback for LLM re-planning."""
        if self._hitl.is_child_hitl(checkpoint):
            raise NotImplementedError(
                "Child HITL modification requires runtime — use HITLHandler directly"
            )
        self._hitl.modify(checkpoint, feedback)

    async def run_until_complete(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        *,
        budgets: dict[str, float] | None = None,
        on_hitl: Callable,
        pid: str | None = None,
        checkpoint: AgentCheckpoint | None = None,
        max_iterations: int = 20,
    ) -> AgentCheckpoint:
        """Run an agent, automatically handling HITL suspensions via a policy.

        Args:
            agent_fn: The agent coroutine.
            budgets: Resource budgets (used on first run only).
            on_hitl: Async callback ``(cp) -> ("approve"|"reject"|"modify", feedback)``.
                     Use built-in policies: ``auto_approve``,
                     ``auto_reject``, ``interactive``.
            pid: Custom process ID.
            checkpoint: Existing checkpoint to resume.
            max_iterations: Safety limit on HITL round-trips.
        """
        for i in range(max_iterations):
            if i == 0 and checkpoint is None:
                cp = await self.run(agent_fn, budgets=budgets, pid=pid)
            else:
                cp = await self.run(agent_fn, checkpoint=checkpoint or cp)
            if cp.status != "SUSPENDED_FOR_HITL":
                return cp
            decision, feedback = await on_hitl(cp)
            if decision == "approve":
                await self.approve(cp)
            elif decision == "reject":
                self.reject(cp, feedback or "")
            elif decision == "modify":
                self.modify(cp, feedback or "")
            else:
                raise ValueError(f"Unknown HITL decision: {decision!r}")
            checkpoint = cp
        raise RuntimeError(
            f"Agent exceeded {max_iterations} HITL iterations without completing"
        )

    async def save(self, checkpoint: AgentCheckpoint) -> None:
        """Persist checkpoint to the configured store."""
        if self._store is None:
            raise RuntimeError("No store configured — pass store= to Castor()")
        self._store.save(checkpoint)

    def load(self, pid: str) -> AgentCheckpoint:
        """Load a checkpoint from the configured store."""
        if self._store is None:
            raise RuntimeError("No store configured — pass store= to Castor()")
        return self._store.load(pid)

    async def run_async(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        *,
        budgets: dict[str, float] | None = None,
        checkpoint: AgentCheckpoint | None = None,
        pid: str | None = None,
    ) -> CastorTask:
        """Run an agent as a background task for preemption support.

        Returns a ``CastorTask`` that can be awaited or preempted via
        ``kernel.preempt(task, reason)``.
        """
        if checkpoint is None:
            if budgets is not None:
                caps = self._cap_mgr.create_capabilities(budgets)
            else:
                caps = {}
            if pid is None:
                pid = f"{agent_fn.__name__}-{uuid.uuid4().hex[:8]}"
            checkpoint = AgentCheckpoint(
                pid=pid,
                status="RUNNING",
                agent_function_name=agent_fn.__name__,
                capabilities=caps,
            )

        runner = AgentRunner(
            self._dam,
            self._cap_mgr,
            lodge=self._lodge,
            agent_registry=self._agent_registry,
            structured_results=self._structured_results,
        )
        task = await runner.run_as_task(agent_fn, checkpoint)
        return CastorTask(runner, task, checkpoint)

    def preempt(
        self,
        castor_task: CastorTask,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Preempt a running agent task."""
        castor_task._runner.preempt(reason, payload)


class CastorTask:
    """Awaitable wrapper around a background agent task.

    Returned by ``kernel.run_async()``.  Holds references to the
    internal runner and asyncio.Task so ``kernel.preempt()`` can cancel it.
    """

    def __init__(
        self,
        runner: AgentRunner,
        task: asyncio.Task,
        checkpoint: AgentCheckpoint,
    ) -> None:
        self._runner = runner
        self._task = task
        self._checkpoint = checkpoint

    @property
    def checkpoint(self) -> AgentCheckpoint:
        """Current checkpoint (live — status updates in real time)."""
        return self._checkpoint

    @property
    def done(self) -> bool:
        """True if the underlying task has finished."""
        return self._task.done()

    def __await__(self):
        return self._wait().__await__()

    async def _wait(self) -> AgentCheckpoint:
        try:
            await self._task
        except asyncio.CancelledError:
            pass  # Preemption — checkpoint already set to PREEMPTED
        return self._checkpoint
