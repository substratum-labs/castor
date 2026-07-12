"""AgentRunner: the kernel-side executor for agent functions."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from castor.models.checkpoint import AgentCheckpoint, SuspendInterrupt
from castor.observability import get_logger
from castor.protocols import (
    ActuatorProtocol,
    AgentRegistryProtocol,
    BudgetProtocol,
    CheckpointStoreProtocol,
    GateProtocol,
    MMUProtocol,
)
from castor.scheduler.proxy import SyscallProxy

_logger = get_logger("castor.scheduler")


class AgentRunner:
    """Runs an agent function as an asyncio.Task.

    Handles three exit modes:
    - Normal completion (agent returns a result)
    - Cooperative suspend (SuspendInterrupt from HITL slow path)
    - Preemption (CancelledError from kernel's task.cancel())

    WARNING: Agent functions MUST NOT make direct network or LLM client calls.
    All non-deterministic operations must go through ``proxy.syscall()``.
    Direct calls bypass the syscall log and break checkpoint/replay
    determinism.  See ``castor.llm.LLMSyscall`` for an LLM-specific wrapper.
    """

    def __init__(
        self,
        gate: GateProtocol,
        capability_manager: BudgetProtocol,
        lodge: MMUProtocol | None = None,
        agent_registry: AgentRegistryProtocol | None = None,
        checkpoint_store: CheckpointStoreProtocol | None = None,
        actuator: ActuatorProtocol | None = None,
        structured_results: bool = False,
        speculative: bool = False,
        scheduler: Any | None = None,
    ) -> None:
        self._gate = gate
        self._budget_mgr = capability_manager
        self._lodge = lodge
        self._agent_registry = agent_registry
        self._checkpoint_store = checkpoint_store
        self._actuator = actuator
        self._structured_results = structured_results
        self._speculative = speculative
        self._scheduler = scheduler
        self._task: asyncio.Task | None = None
        self._current_checkpoint: AgentCheckpoint | None = None

    async def run(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        checkpoint: AgentCheckpoint,
    ) -> AgentCheckpoint:
        """Execute an agent function directly (not as a background task).

        Returns the updated checkpoint after completion, suspension, or preemption.
        """
        self._current_checkpoint = checkpoint
        if self._checkpoint_store is not None:
            self._checkpoint_store.save(checkpoint)

        if checkpoint.pending_commit is not None:
            status = (
                self._actuator.query_status(checkpoint.pending_commit)
                if self._actuator is not None
                else "UNKNOWN"
            )
            checkpoint.pending_commit_status = status
            checkpoint.status = "SUSPENDED"
            if self._checkpoint_store is not None:
                self._checkpoint_store.save(checkpoint)
            return checkpoint

        checkpoint.status = "RUNNING"
        _logger.info(
            "agent_start",
            extra={
                "pid": checkpoint.pid,
                "agent": checkpoint.agent_function_name,
            },
        )
        kernel_tools = self._lodge.kernel_tool_names if self._lodge else set()
        proxy = SyscallProxy(
            checkpoint,
            self._gate,
            self._budget_mgr,
            lodge=self._lodge,
            kernel_tool_names=kernel_tools,
            agent_registry=self._agent_registry,
            checkpoint_store=self._checkpoint_store,
            structured_results=self._structured_results,
            speculative=self._speculative,
            scheduler=self._scheduler,
        )

        # Set ContextVar so castor.lib functions work (both new and legacy agents)
        from castor.lib._context import set_proxy

        set_proxy(proxy)

        # Detect agent signature: 0 required params = new-style, 1+ = legacy
        sig = inspect.signature(agent_fn)
        required = [
            p
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]

        try:
            if len(required) == 0:
                checkpoint.result = await agent_fn()
            else:
                checkpoint.result = await agent_fn(proxy)
            checkpoint.status = "COMPLETED"
            # Clear stale preemption context on successful completion so it
            # doesn't leak into future run cycles on the same checkpoint.
            checkpoint.preemption_reason = None
            checkpoint.preemption_payload = None
            checkpoint.partial_work = None
        except SuspendInterrupt:
            # Cooperative suspend — checkpoint already set by proxy
            pass
        except asyncio.CancelledError:
            checkpoint.status = "PREEMPTED"
            # checkpoint.syscall_log is consistent up to last completed syscall
            # preemption_reason/payload were set before cancel()
            raise

        finally:
            if self._checkpoint_store is not None:
                self._checkpoint_store.save(checkpoint)

        _logger.info(
            "agent_complete",
            extra={
                "pid": checkpoint.pid,
                "status": checkpoint.status,
            },
        )
        return checkpoint

    async def run_as_task(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        checkpoint: AgentCheckpoint,
    ) -> asyncio.Task:
        """Wrap run() in an asyncio.Task for background execution.

        Returns the Task object so the kernel can cancel it for preemption.
        """
        self._current_checkpoint = checkpoint
        self._task = asyncio.create_task(self.run(agent_fn, checkpoint))
        return self._task

    def preempt(self, reason: str, payload: dict | None = None) -> None:
        """Kernel calls this to preempt the agent immediately.

        Sets preemption context on the checkpoint, then cancels the task.
        """
        if self._task and not self._task.done():
            if self._current_checkpoint:
                self._current_checkpoint.preemption_reason = reason
                self._current_checkpoint.preemption_payload = payload
            self._task.cancel()


def default_runner_factory(
    gate: GateProtocol,
    capability_manager: BudgetProtocol,
    *,
    lodge: MMUProtocol | None = None,
    agent_registry: AgentRegistryProtocol | None = None,
    structured_results: bool = False,
    speculative: bool = False,
    **kwargs: Any,
) -> AgentRunner:
    """Create an AgentRunner with standard parameters.

    This is the default ``runner_factory`` used by ``Castor()`` when no
    custom factory is provided.  Pollux (or other runtimes) can supply
    their own factory that returns a different ``RunnerProtocol`` implementation.
    """
    return AgentRunner(
        gate,
        capability_manager,
        lodge=lodge,
        agent_registry=agent_registry,
        checkpoint_store=kwargs.get("checkpoint_store"),
        actuator=kwargs.get("actuator"),
        structured_results=structured_results,
        speculative=speculative,
        scheduler=kwargs.get("scheduler"),
    )
