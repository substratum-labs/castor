"""AgentRunner: the kernel-side executor for agent functions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from castor.capability.manager import CapabilityManager
from castor.dam.validator import CastorDam
from castor.models.checkpoint import AgentCheckpoint, SuspendInterrupt
from castor.stream.proxy import SyscallProxy


class AgentRunner:
    """Runs an agent function as an asyncio.Task.

    Handles three exit modes:
    - Normal completion (agent returns a result)
    - Cooperative suspend (SuspendInterrupt from HITL slow path)
    - Preemption (CancelledError from kernel's task.cancel())
    """

    def __init__(self, dam: CastorDam, capability_manager: CapabilityManager) -> None:
        self._dam = dam
        self._cap_mgr = capability_manager
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
        checkpoint.status = "RUNNING"
        proxy = SyscallProxy(checkpoint, self._dam, self._cap_mgr)

        try:
            await agent_fn(proxy)
            checkpoint.status = "COMPLETED"
        except SuspendInterrupt:
            # Cooperative suspend — checkpoint already set by proxy
            pass
        except asyncio.CancelledError:
            checkpoint.status = "PREEMPTED"
            # checkpoint.syscall_log is consistent up to last completed syscall
            # preemption_reason/payload were set before cancel()
            raise

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
