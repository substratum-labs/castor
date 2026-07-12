"""HITL (Human-in-the-Loop) feedback handler."""

from __future__ import annotations

from collections.abc import Callable

from castor.kernel.journal import InMemoryJournal
from castor.models.checkpoint import (
    AgentCheckpoint,
    SyscallRecord,
    compute_invocation_id,
)
from castor.protocols import (
    AgentRegistryProtocol,
    BudgetProtocol,
    GateProtocol,
    MMUProtocol,
    RunnerProtocol,
)


class HITLHandler:
    """Processes human feedback on suspended checkpoints.

    Three modes: approve (execute as-is), reject (block with feedback),
    modify (log feedback so LLM re-plans on replay).
    """

    def __init__(
        self,
        runner_factory: Callable[..., RunnerProtocol] | None = None,
    ) -> None:
        self._runner_factory = runner_factory

    async def approve(
        self,
        checkpoint: AgentCheckpoint,
        gate: GateProtocol,
        capability_manager: BudgetProtocol,
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
        validated = gate.validate(tool_name, arguments)
        tool_meta = gate.get_tool_meta(tool_name)

        if tool_meta.cost_per_use > 0:
            capability_manager.deduct(
                checkpoint.capabilities,
                tool_meta.consumes,
                tool_meta.cost_per_use,
            )

        journal = InMemoryJournal(checkpoint.syscall_log)
        inv_id = compute_invocation_id(
            checkpoint.pid,
            len(journal),
            tool_name,
            arguments,
        )
        result = await gate.execute(
            tool_name,
            {**validated, "operation_id": inv_id},
        )
        journal.append(
            SyscallRecord(
                request=request,
                response=result,
                was_hitl=True,
                invocation_id=inv_id,
            )
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

        request = checkpoint.pending_hitl
        journal = InMemoryJournal(checkpoint.syscall_log)
        inv_id = compute_invocation_id(
            checkpoint.pid,
            len(journal),
            request.get("tool_name", ""),
            request.get("arguments", {}),
        )
        journal.append(
            SyscallRecord(
                request=request,
                response={
                    "status": "HITL_REJECTED",
                    "human_feedback": feedback,
                },
                was_hitl=True,
                invocation_id=inv_id,
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

        request = checkpoint.pending_hitl
        journal = InMemoryJournal(checkpoint.syscall_log)
        inv_id = compute_invocation_id(
            checkpoint.pid,
            len(journal),
            request.get("tool_name", ""),
            request.get("arguments", {}),
        )
        journal.append(
            SyscallRecord(
                request=request,
                response={
                    "status": "HITL_MODIFIED",
                    "human_feedback": feedback,
                },
                was_hitl=True,
                invocation_id=inv_id,
            )
        )
        checkpoint.pending_hitl = None
        checkpoint.status = "RUNNING"

    # ── Child HITL (spawn_agent propagation) ──

    def is_child_hitl(self, checkpoint: AgentCheckpoint) -> bool:
        """Check if the pending HITL belongs to a child spawn."""
        if checkpoint.pending_hitl is None:
            return False
        return checkpoint.pending_hitl.get("tool_name") in {
            "spawn_agent",
            "join_agent",
        }

    async def approve_child_hitl(
        self,
        checkpoint: AgentCheckpoint,
        gate: GateProtocol,
        capability_manager: BudgetProtocol,
        agent_registry: AgentRegistryProtocol,
        lodge: MMUProtocol | None = None,
    ) -> None:
        """Approve HITL on a child's suspended syscall, then resume child."""
        if not self.is_child_hitl(checkpoint):
            raise ValueError("No child spawn HITL pending — use approve()")

        child_cp = self._get_child_checkpoint(checkpoint)
        await self.approve(child_cp, gate, capability_manager)
        await self._resume_child(
            checkpoint,
            child_cp,
            gate,
            capability_manager,
            agent_registry,
            lodge=lodge,
        )

    async def reject_child_hitl(
        self,
        checkpoint: AgentCheckpoint,
        feedback: str,
        gate: GateProtocol,
        capability_manager: BudgetProtocol,
        agent_registry: AgentRegistryProtocol,
        lodge: MMUProtocol | None = None,
    ) -> None:
        """Reject a child's pending HITL and resume child."""
        if not self.is_child_hitl(checkpoint):
            raise ValueError("No child spawn HITL pending — use reject()")

        child_cp = self._get_child_checkpoint(checkpoint)
        self.reject(child_cp, feedback)
        await self._resume_child(
            checkpoint,
            child_cp,
            gate,
            capability_manager,
            agent_registry,
            lodge=lodge,
        )

    async def modify_child_hitl(
        self,
        checkpoint: AgentCheckpoint,
        feedback: str,
        gate: GateProtocol,
        capability_manager: BudgetProtocol,
        agent_registry: AgentRegistryProtocol,
        lodge: MMUProtocol | None = None,
    ) -> None:
        """Modify a child's pending HITL and resume child."""
        if not self.is_child_hitl(checkpoint):
            raise ValueError("No child spawn HITL pending — use modify()")

        child_cp = self._get_child_checkpoint(checkpoint)
        self.modify(child_cp, feedback)
        await self._resume_child(
            checkpoint,
            child_cp,
            gate,
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
        gate: GateProtocol,
        capability_manager: BudgetProtocol,
        agent_registry: AgentRegistryProtocol,
        lodge: MMUProtocol | None = None,
    ) -> None:
        """Replay a child agent after its HITL was resolved."""
        import asyncio

        agent_fn = agent_registry.get(child_cp.agent_function_name)

        if self._runner_factory is not None:
            runner = self._runner_factory(
                gate, capability_manager, lodge=lodge, agent_registry=agent_registry
            )
        else:
            from castor.scheduler.runner import AgentRunner

            runner = AgentRunner(
                gate, capability_manager, lodge=lodge, agent_registry=agent_registry
            )

        try:
            child_cp = await runner.run(agent_fn, child_cp)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Child crashed — mark failed, reclaim budget, unblock parent
            child_cp.status = "FAILED"
            capability_manager.reclaim(parent_cp.capabilities, child_cp.capabilities)
            last = parent_cp.syscall_log[-1]
            last.child_checkpoint = child_cp
            last.response = None
            parent_cp.pending_hitl = None
            parent_cp.status = "RUNNING"
            return

        last = parent_cp.syscall_log[-1]
        last.child_checkpoint = child_cp

        if child_cp.status == "SUSPENDED_FOR_HITL":
            # Child suspended again — parent stays suspended
            return

        # Child completed — reclaim budget and update parent
        capability_manager.reclaim(parent_cp.capabilities, child_cp.capabilities)
        last.response = child_cp.result
        parent_cp.pending_hitl = None
        parent_cp.status = "RUNNING"
