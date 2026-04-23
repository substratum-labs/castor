"""Preemptive scheduling models — Phase A.

Between-syscall preemption: the kernel can preempt a running agent
at the next syscall boundary. The agent sees ``PreemptedError``
propagating up from ``proxy.syscall()``.

``PreemptedError`` inherits from ``BaseException`` (not ``Exception``)
so that generic ``except Exception:`` blocks in agent code do NOT
silently swallow preemption signals.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class PreemptionReason(StrEnum):
    """Why the kernel preempted an agent."""

    # Phase A (active)
    BUDGET_EXHAUSTED = "budget_exhausted"
    SPECULATIVE_LOSER = "speculative_loser"
    PRIORITY_PREEMPTED = "priority_preempted"

    # Phase B (reserved — declared now, not raised yet)
    OPERATOR_KILL = "operator_kill"
    DEADLINE = "deadline"
    PARENT_KILL = "parent_kill"


class PreemptedError(BaseException):
    """Raised inside an agent when the kernel preempts execution.

    Inherits from ``BaseException`` (not ``Exception``) so that generic
    ``except Exception:`` blocks in agent code do not silently swallow
    preemption. Agents that need cleanup before exit must explicitly
    catch ``PreemptedError`` or ``BaseException``.

    Phase A reasons: BUDGET_EXHAUSTED, SPECULATIVE_LOSER,
    PRIORITY_PREEMPTED.
    """

    def __init__(
        self,
        reason: PreemptionReason,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.metadata = metadata or {}
        super().__init__(f"agent preempted: {reason.value}")
