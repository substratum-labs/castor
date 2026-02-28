"""HITL (Human-in-the-Loop) feedback handler."""

from __future__ import annotations

from castor.capability.manager import CapabilityManager
from castor.dam.validator import CastorDam
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord


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
