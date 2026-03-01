"""HITL (Human-in-the-Loop) feedback handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from castor.capability.manager import CapabilityManager
from castor.dam.validator import CastorDam
from castor.models.checkpoint import AgentCheckpoint, SuspendInterrupt, SyscallRecord

if TYPE_CHECKING:
    from castor.lodge.core import CastorLodge
    from castor.stream.agent_registry import AgentRegistry


class HITLHandler:
    """Processes human feedback on suspended checkpoints.

    Three modes: approve (execute as-is), reject (block with feedback),
    modify (log feedback so LLM re-plans on replay).
    """

    async def approve(
        self,
        checkpoint: AgentCheckpoint,
        dam: CastorDam,
        capability_manager: CapabilityManager,
    ) -> None:
        """Approve the pending HITL syscall and execute it.

        1. Execute the blocked syscall now
        2. Append result to syscall_log with was_hitl=True
        3. Clear pending_hitl, set status=RUNNING
        """
        if checkpoint.pending_hitl is None:
            raise ValueError("No pending HITL request to approve")

        request = checkpoint.pending_hitl
        tool_name = request["tool_name"]
        arguments = request["arguments"]

        # Validate and execute
        validated = dam.validate(tool_name, arguments)
        tool_meta = dam.get_tool_meta(tool_name)

        capability_manager.deduct(
            checkpoint.capabilities,
            tool_meta.consumes,
            tool_meta.cost_per_use,
        )

        result = await dam.execute(tool_name, validated)

        checkpoint.syscall_log.append(
            SyscallRecord(request=request, response=result, was_hitl=True)
        )
        checkpoint.pending_hitl = None
        checkpoint.status = "RUNNING"

    def reject(self, checkpoint: AgentCheckpoint, feedback: str) -> None:
        """Reject the pending HITL syscall with human feedback.

        Logs HITL_REJECTED so the LLM sees the rejection on replay
        and can re-plan accordingly.
        """
        if checkpoint.pending_hitl is None:
            raise ValueError("No pending HITL request to reject")

        checkpoint.syscall_log.append(
            SyscallRecord(
                request=checkpoint.pending_hitl,
                response={
                    "status": "HITL_REJECTED",
                    "human_feedback": feedback,
                },
                was_hitl=True,
            )
        )
        checkpoint.pending_hitl = None
        checkpoint.status = "RUNNING"

    def modify(self, checkpoint: AgentCheckpoint, feedback: str) -> None:
        """Approve with modification — log feedback for LLM re-planning.

        The original request is logged with HITL_MODIFIED status and
        the human's natural language feedback. On replay, the LLM sees
        this feedback and issues a revised syscall.
        """
        if checkpoint.pending_hitl is None:
            raise ValueError("No pending HITL request to modify")

        checkpoint.syscall_log.append(
            SyscallRecord(
                request=checkpoint.pending_hitl,
                response={
                    "status": "HITL_MODIFIED",
                    "human_feedback": feedback,
                },
                was_hitl=True,
            )
        )
        checkpoint.pending_hitl = None
        checkpoint.status = "RUNNING"

    # ── Child HITL (spawn_agent propagation) ──

    def is_child_hitl(self, checkpoint: AgentCheckpoint) -> bool:
        """Check if the pending HITL belongs to a child spawn."""
        if checkpoint.pending_hitl is None:
            return False
        return checkpoint.pending_hitl.get("tool_name") == "spawn_agent"

    async def approve_child_hitl(
        self,
        checkpoint: AgentCheckpoint,
        dam: CastorDam,
        capability_manager: CapabilityManager,
        agent_registry: AgentRegistry,
        lodge: CastorLodge | None = None,
    ) -> None:
        """Approve HITL on a child's suspended syscall, then resume child."""
        if not self.is_child_hitl(checkpoint):
            raise ValueError("No child spawn HITL pending — use approve()")

        child_cp = self._get_child_checkpoint(checkpoint)
        await self.approve(child_cp, dam, capability_manager)
        await self._resume_child(
            checkpoint,
            child_cp,
            dam,
            capability_manager,
            agent_registry,
            lodge=lodge,
        )

    async def reject_child_hitl(
        self,
        checkpoint: AgentCheckpoint,
        feedback: str,
        dam: CastorDam,
        capability_manager: CapabilityManager,
        agent_registry: AgentRegistry,
        lodge: CastorLodge | None = None,
    ) -> None:
        """Reject a child's pending HITL and resume child."""
        if not self.is_child_hitl(checkpoint):
            raise ValueError("No child spawn HITL pending — use reject()")

        child_cp = self._get_child_checkpoint(checkpoint)
        self.reject(child_cp, feedback)
        await self._resume_child(
            checkpoint,
            child_cp,
            dam,
            capability_manager,
            agent_registry,
            lodge=lodge,
        )

    async def modify_child_hitl(
        self,
        checkpoint: AgentCheckpoint,
        feedback: str,
        dam: CastorDam,
        capability_manager: CapabilityManager,
        agent_registry: AgentRegistry,
        lodge: CastorLodge | None = None,
    ) -> None:
        """Modify a child's pending HITL and resume child."""
        if not self.is_child_hitl(checkpoint):
            raise ValueError("No child spawn HITL pending — use modify()")

        child_cp = self._get_child_checkpoint(checkpoint)
        self.modify(child_cp, feedback)
        await self._resume_child(
            checkpoint,
            child_cp,
            dam,
            capability_manager,
            agent_registry,
            lodge=lodge,
        )

    def _get_child_checkpoint(self, checkpoint: AgentCheckpoint) -> AgentCheckpoint:
        """Extract child checkpoint from parent's last syscall record."""
        last = checkpoint.syscall_log[-1]
        if last.child_checkpoint is None:
            raise ValueError("No child checkpoint in last syscall record")
        return last.child_checkpoint

    async def _resume_child(
        self,
        parent_cp: AgentCheckpoint,
        child_cp: AgentCheckpoint,
        dam: CastorDam,
        capability_manager: CapabilityManager,
        agent_registry: AgentRegistry,
        lodge: CastorLodge | None = None,
    ) -> None:
        """Replay a child agent after its HITL was resolved."""
        from castor.stream.proxy import SyscallProxy

        agent_fn = agent_registry.get(child_cp.agent_function_name)
        kernel_tool_names = lodge.kernel_tool_names if lodge else set()
        child_proxy = SyscallProxy(
            checkpoint=child_cp,
            dam=dam,
            capability_manager=capability_manager,
            lodge=lodge,
            kernel_tool_names=kernel_tool_names,
            agent_registry=agent_registry,
        )

        try:
            child_result = await agent_fn(child_proxy)
            child_cp.result = child_result
            child_cp.status = "COMPLETED"
        except SuspendInterrupt:
            # Child suspended again — parent stays suspended
            last = parent_cp.syscall_log[-1]
            last.child_checkpoint = child_cp
            return

        # Child completed — reclaim budget and update parent
        capability_manager.reclaim(parent_cp.capabilities, child_cp.capabilities)
        last = parent_cp.syscall_log[-1]
        last.response = child_cp.result
        last.child_checkpoint = child_cp
        parent_cp.pending_hitl = None
        parent_cp.status = "RUNNING"
