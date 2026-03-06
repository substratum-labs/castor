"""Castor: the unified kernel facade."""

from __future__ import annotations

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
    ) -> None:
        # ── Dam (tool validation + execution) ──
        if dam is not None:
            self._dam = dam
        elif tools is not None:
            registry = ToolRegistry()
            for func in tools:
                meta = getattr(func, "_castor_metadata", None)
                if meta is None:
                    raise TypeError(f"{func!r} is not decorated with @castor_tool")
                registry.register(meta)
            self._dam = CastorDam(registry)
        else:
            self._dam = CastorDam(default_registry)

        # ── Capability Manager ──
        self._cap_mgr = capability_manager or CapabilityManager()

        # ── Optional subsystems ──
        self._lodge = lodge
        self._agent_registry = agent_registry
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
