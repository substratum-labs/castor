"""Preemptive scheduler — Phase A (between-syscall).

Pull-model: called by ``SyscallProxy.syscall()`` before dispatch.
Returns ``None`` (proceed) or ``(PreemptionReason, metadata)``
(caller raises ``PreemptedError``).

Phase A checks are all deterministic — same checkpoint state always
produces the same decision. Phase B will add a watchdog task that
pushes signals (deadline, operator kill).
"""

from __future__ import annotations

from typing import Any

from castor.models.checkpoint import AgentCheckpoint
from castor.models.preemption import PreemptionReason
from castor.protocols import BudgetProtocol


class Scheduler:
    """Decides at each syscall entry whether to preempt the running agent."""

    def __init__(
        self,
        budget_mgr: BudgetProtocol | None = None,
    ) -> None:
        self._budget_mgr = budget_mgr
        # Fork losers: pid → {winner_pid, group_id}
        self._fork_losers: dict[str, dict[str, str]] = {}
        # Pending children by parent pid: priority queue tracking
        self._pending_children: dict[str, list[tuple[int, str]]] = {}

    def should_preempt(
        self,
        checkpoint: AgentCheckpoint,
    ) -> tuple[PreemptionReason, dict[str, Any]] | None:
        """Returns ``(reason, metadata)`` or ``None``.

        Order of checks (first match wins, deterministic):
          1. Budget overshoot (any capability has current_usage > max_budget)
          2. Speculative fork loser (marked by fork resolver)
          3. Higher-priority sibling pending
        """
        # 1. Budget exhaustion
        for name, budget in checkpoint.capabilities.items():
            if budget.current_usage > budget.max_budget:
                return (
                    PreemptionReason.BUDGET_EXHAUSTED,
                    {
                        "resource": name,
                        "usage": budget.current_usage,
                        "max": budget.max_budget,
                    },
                )

        # 2. Speculative fork loser
        if checkpoint.pid in self._fork_losers:
            info = self._fork_losers.pop(checkpoint.pid)
            return (
                PreemptionReason.SPECULATIVE_LOSER,
                info,
            )

        # 3. Priority preemption (higher-priority sibling pending)
        parent = checkpoint.parent_pid
        if parent and parent in self._pending_children:
            pending = self._pending_children[parent]
            for pri, pid in pending:
                if pri > checkpoint.priority and pid != checkpoint.pid:
                    return (
                        PreemptionReason.PRIORITY_PREEMPTED,
                        {
                            "higher_priority_pid": pid,
                            "higher_priority": pri,
                            "current_priority": checkpoint.priority,
                        },
                    )

        return None

    def mark_fork_loser(self, pid: str, winner_pid: str, group_id: str = "") -> None:
        """Mark a pid as a speculative fork loser.

        Its next ``proxy.syscall()`` call will raise
        ``PreemptedError(SPECULATIVE_LOSER)``.
        """
        self._fork_losers[pid] = {
            "winner_pid": winner_pid,
            "group_id": group_id,
        }

    def register_pending_child(
        self, parent_pid: str, child_pid: str, priority: int
    ) -> None:
        """Track a pending child for priority comparison."""
        if parent_pid not in self._pending_children:
            self._pending_children[parent_pid] = []
        self._pending_children[parent_pid].append((priority, child_pid))
        # Keep sorted by priority descending
        self._pending_children[parent_pid].sort(key=lambda x: -x[0])

    def unregister_child(self, parent_pid: str, child_pid: str) -> None:
        """Remove a completed child from tracking."""
        if parent_pid in self._pending_children:
            self._pending_children[parent_pid] = [
                (p, pid)
                for p, pid in self._pending_children[parent_pid]
                if pid != child_pid
            ]
            if not self._pending_children[parent_pid]:
                del self._pending_children[parent_pid]
