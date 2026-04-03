"""Castor: the unified kernel facade."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from castor.capability.manager import CapabilityManager
from castor.gate.registry import ToolMetadata, ToolRegistry, default_registry
from castor.gate.validator import SyscallGate
from castor.kernel.journal import InMemoryJournal
from castor.kernel.summary import ExecutionSummary, scan_journal
from castor.models.checkpoint import AgentCheckpoint
from castor.protocols import BudgetProtocol, GateProtocol, RunnerProtocol
from castor.scheduler.hitl import HITLHandler
from castor.scheduler.proxy import SyscallProxy
from castor.scheduler.runner import default_runner_factory


class Castor:
    """Unified kernel facade -- assembles all subsystems behind a single object.

    Usage::

        kernel = Castor()
        cp = await kernel.run(my_agent, budgets={"api": 50.0})
    """

    def __init__(
        self,
        *,
        tools: list[Callable] | None = None,
        destructive: list[str] | None = None,
        llm: Callable | None = None,
        llm_cost: float = 1.0,
        llm_resource: str = "api",
        lodge: Any | None = None,
        agent_registry: Any | None = None,
        store: str | Any | None = None,
        gate: GateProtocol | None = None,
        capability_manager: BudgetProtocol | None = None,
        budgets: dict[str, float] | None = None,
        default_budgets: dict[str, float] | None = None,
        auto_budget: float | None = None,
        structured_results: bool = False,
        roche: bool | dict[str, Any] | None = None,
        runner_factory: Callable[..., RunnerProtocol] | None = None,
    ) -> None:
        # -- Roche sandbox integration --
        if roche is not None:
            roche_tools = self._init_roche(roche)
            tools = (tools or []) + roche_tools

        # -- Gate (tool validation + execution) --
        if gate is not None:
            self._gate = gate
        elif tools is not None:
            from castor.llm.wrapper import LLMSyscall, StreamingLLMSyscall

            destructive_set = set(destructive or [])
            registry = ToolRegistry()
            for item in tools:
                if isinstance(item, (LLMSyscall, StreamingLLMSyscall)):
                    registry.register(item._metadata)
                elif isinstance(item, tuple) and len(item) == 2:
                    # (name, func) tuple — register with explicit name
                    name, func = item
                    if not callable(func):
                        raise TypeError(f"{func!r} is not callable")
                    is_destructive = name in destructive_set
                    meta = ToolMetadata.from_function(
                        func,
                        destructive=is_destructive,
                        requires_hitl=is_destructive,
                    )
                    meta.tool_name = name
                    registry.register(meta)
                else:
                    meta = getattr(item, "_castor_metadata", None)
                    if meta is None:
                        # Plain function — auto-wrap with ToolMetadata
                        if not callable(item):
                            raise TypeError(f"{item!r} is not callable")
                        fn_name = getattr(item, "__name__", repr(item))
                        is_destructive = fn_name in destructive_set
                        meta = ToolMetadata.from_function(
                            item,
                            destructive=is_destructive,
                            requires_hitl=is_destructive,
                        )
                    registry.register(meta)

            # Auto-wrap LLM callable as a tracked syscall
            if llm is not None:
                llm_syscall = LLMSyscall(
                    registry=registry,
                    call_fn=llm,
                    consumes=llm_resource,
                    cost_per_use=llm_cost,
                )
                self._llm_syscall = llm_syscall
            else:
                self._llm_syscall = None

            self._gate = SyscallGate(registry)
        else:
            self._gate = SyscallGate(default_registry)
            self._llm_syscall = None

        # -- Capability Manager --
        self._cap_mgr = capability_manager or CapabilityManager()

        # -- Optional subsystems --
        self._lodge = lodge
        if agent_registry is not None:
            self._agent_registry = agent_registry
        else:
            from castor.scheduler.agent_registry import default_agent_registry

            if default_agent_registry.list_agents():
                self._agent_registry = default_agent_registry
            else:
                self._agent_registry = None
        self._default_budgets = budgets or default_budgets
        self._auto_budget = auto_budget
        self._structured_results = structured_results
        self._runner_factory = runner_factory or default_runner_factory
        self._hitl = HITLHandler(runner_factory=self._runner_factory)

        # -- Persistence --
        self._store = None
        if store is not None:
            from castor.scheduler.persistence import CheckpointStore

            if isinstance(store, str):
                self._store = CheckpointStore(store)
            else:
                self._store = store

    @staticmethod
    def _init_roche(roche: bool | dict[str, Any]) -> list[Callable]:
        """Initialize Roche sandbox tools.

        Args:
            roche: True for defaults, or a dict of kwargs for RocheCastorBridge.

        Returns:
            List of @castor_tool-decorated functions.
        """
        try:
            from roche_sandbox.castor import roche_tools
        except ImportError:
            raise ImportError(
                "Castor(roche=...) requires the 'roche-sandbox' package. "
                "Install with: pip install roche-sandbox[castor]"
            ) from None

        kwargs = roche if isinstance(roche, dict) else {}
        return roche_tools(**kwargs)

    # -- Public properties --

    @property
    def gate(self) -> GateProtocol:
        """The SyscallGate (tool validation + execution engine)."""
        return self._gate

    @property
    def capability_manager(self) -> BudgetProtocol:
        """The CapabilityManager (budget tracking)."""
        return self._cap_mgr

    @property
    def store(self) -> Any | None:
        """The configured checkpoint store, or None."""
        return self._store

    @property
    def llm(self) -> Any | None:
        """The LLM syscall wrapper, or None if no llm= was provided."""
        return self._llm_syscall

    # -- Internal helpers --

    def _resolve_budgets(
        self, budgets: dict[str, float] | None
    ) -> dict[str, float] | None:
        """Return explicit budgets, fall back to default_budgets, or auto-infer."""
        if budgets is not None:
            return budgets
        if self._default_budgets is not None:
            return self._default_budgets
        if self._auto_budget is not None:
            return self._infer_budgets()
        return None

    def _infer_budgets(self) -> dict[str, float] | None:
        """Auto-create budgets from tool metadata.

        Scans registered tools for resource types (``consumes``) and creates
        a budget of ``auto_budget`` for each unique resource type.
        """
        resource_types: set[str] = set()
        for name in self._gate.list_tools():
            meta = self._gate.get_tool_meta(name)
            if meta.cost_per_use > 0:
                resource_types.add(meta.consumes)
        if not resource_types:
            return None
        return {rt: self._auto_budget for rt in resource_types}

    def _make_checkpoint(
        self,
        agent_fn: Callable,
        budgets: dict[str, float] | None,
        pid: str | None,
    ) -> AgentCheckpoint:
        effective = self._resolve_budgets(budgets)
        caps = self._cap_mgr.create_capabilities(effective) if effective else {}
        if pid is None:
            pid = f"{agent_fn.__name__}-{uuid.uuid4().hex[:8]}"
        return AgentCheckpoint(
            pid=pid,
            status="RUNNING",
            agent_function_name=agent_fn.__name__,
            capabilities=caps,
        )

    # -- Execution --

    async def run(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        *,
        budgets: dict[str, float] | None = None,
        checkpoint: AgentCheckpoint | None = None,
        pid: str | None = None,
        speculative: bool = False,
    ) -> AgentCheckpoint:
        """Run an agent function.

        Args:
            agent_fn: The agent coroutine ``async def agent(proxy) -> result``.
            budgets: Resource budgets like ``{"api": 50.0}``.
                     Not provided = falls back to ``default_budgets``.
                     Neither provided = unlimited (no budget enforcement).
            checkpoint: Pass an existing checkpoint to resume (e.g. after HITL).
            pid: Custom process ID. Auto-generated if not provided.
            speculative: If True, skip HITL suspensions — agent runs freely
                         in sandbox, reviewed after completion.
        """
        if checkpoint is None:
            checkpoint = self._make_checkpoint(agent_fn, budgets, pid)

        runner = self._runner_factory(
            self._gate,
            self._cap_mgr,
            lodge=self._lodge,
            agent_registry=self._agent_registry,
            structured_results=self._structured_results,
            speculative=speculative,
        )
        return await runner.run(agent_fn, checkpoint)

    def _resolve_checkpoint(
        self, ref: AgentCheckpoint | str
    ) -> tuple[AgentCheckpoint, bool]:
        """Resolve a checkpoint reference (object or PID string).

        Returns:
            (checkpoint, from_store) — from_store is True when loaded by PID.
        """
        if isinstance(ref, str):
            if self._store is None:
                raise RuntimeError(
                    "PID-based HITL requires a store -- pass store= to Castor()"
                )
            return self._store.load(ref), True
        return ref, False

    def _auto_save(self, checkpoint: AgentCheckpoint, from_store: bool) -> None:
        """Persist checkpoint back to store if it was loaded by PID."""
        if from_store:
            self._store.save(checkpoint)

    async def approve(self, checkpoint: AgentCheckpoint | str) -> None:
        """Approve a pending HITL syscall.

        Args:
            checkpoint: An ``AgentCheckpoint`` object or a PID string.
                        When a PID is passed, the checkpoint is loaded
                        from the configured store and saved back after approval.
        """
        checkpoint, from_store = self._resolve_checkpoint(checkpoint)
        if self._hitl.is_child_hitl(checkpoint):
            if self._agent_registry is None:
                raise RuntimeError(
                    "Child HITL approval requires an agent_registry on Castor"
                )
            await self._hitl.approve_child_hitl(
                checkpoint,
                self._gate,
                self._cap_mgr,
                self._agent_registry,
                lodge=self._lodge,
            )
        else:
            await self._hitl.approve(checkpoint, self._gate, self._cap_mgr)
        self._auto_save(checkpoint, from_store)

    def reject(self, checkpoint: AgentCheckpoint | str, reason: str) -> None:
        """Reject a pending HITL syscall with feedback.

        Args:
            checkpoint: An ``AgentCheckpoint`` object or a PID string.
            reason: Human-readable rejection reason for LLM feedback.
        """
        checkpoint, from_store = self._resolve_checkpoint(checkpoint)
        if self._hitl.is_child_hitl(checkpoint):
            raise NotImplementedError(
                "Child HITL rejection requires runtime -- use HITLHandler directly"
            )
        self._hitl.reject(checkpoint, reason)
        self._auto_save(checkpoint, from_store)

    def modify(self, checkpoint: AgentCheckpoint | str, feedback: str) -> None:
        """Approve with modification -- log feedback for LLM re-planning.

        Args:
            checkpoint: An ``AgentCheckpoint`` object or a PID string.
            feedback: Natural language feedback for the LLM to re-plan.
        """
        checkpoint, from_store = self._resolve_checkpoint(checkpoint)
        if self._hitl.is_child_hitl(checkpoint):
            raise NotImplementedError(
                "Child HITL modification requires runtime -- use HITLHandler directly"
            )
        self._hitl.modify(checkpoint, feedback)
        self._auto_save(checkpoint, from_store)

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
            raise RuntimeError("No store configured -- pass store= to Castor()")
        self._store.save(checkpoint)

    def load(self, pid: str) -> AgentCheckpoint:
        """Load a checkpoint from the configured store."""
        if self._store is None:
            raise RuntimeError("No store configured -- pass store= to Castor()")
        return self._store.load(pid)

    def scan(self, checkpoint: AgentCheckpoint) -> ExecutionSummary:
        """Scan a checkpoint's journal and produce an execution summary.

        Use after ``run(speculative=True)`` to review what the agent did.
        Returns flagged steps (destructive/HITL tools) and statistics.
        """
        journal = InMemoryJournal(checkpoint.syscall_log)
        return scan_journal(journal, self._gate)

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
            checkpoint = self._make_checkpoint(agent_fn, budgets, pid)

        runner = self._runner_factory(
            self._gate,
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
        runner: RunnerProtocol,
        task: asyncio.Task,
        checkpoint: AgentCheckpoint,
    ) -> None:
        self._runner = runner
        self._task = task
        self._checkpoint = checkpoint

    @property
    def checkpoint(self) -> AgentCheckpoint:
        """Current checkpoint (live -- status updates in real time)."""
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
            pass  # Preemption -- checkpoint already set to PREEMPTED
        return self._checkpoint
