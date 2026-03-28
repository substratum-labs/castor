"""Execution summary — scan a Journal for review after speculative execution.

Generates a human-readable summary of what the agent did, flagging
destructive or HITL-requiring steps for review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from castor.gate.registry import ToolMetadata
    from castor.protocols import GateProtocol, JournalProtocol


@dataclass
class FlaggedStep:
    """A step flagged for human review."""

    index: int
    tool_name: str
    arguments: dict[str, Any]
    response: Any
    reason: str


@dataclass
class ExecutionSummary:
    """Summary of a speculative execution for human review."""

    total_steps: int
    auto_verified: int
    flagged: list[FlaggedStep] = field(default_factory=list)
    tools_used: dict[str, int] = field(default_factory=dict)

    @property
    def flagged_count(self) -> int:
        return len(self.flagged)


def scan_journal(
    journal: JournalProtocol,
    gate: GateProtocol,
) -> ExecutionSummary:
    """Scan a completed Journal and produce an execution summary.

    Flags steps involving destructive or HITL-requiring tools.
    Auto-verifies everything else.

    Args:
        journal: The completed execution journal.
        gate: Gate for looking up tool metadata (destructive, requires_hitl).

    Returns:
        ExecutionSummary with flagged steps and statistics.
    """
    flagged: list[FlaggedStep] = []
    tools_used: dict[str, int] = {}

    for idx, record in journal.scan_from(0):
        tool_name = record.request.get("tool_name", "unknown")
        tools_used[tool_name] = tools_used.get(tool_name, 0) + 1

        # Check if this tool should be flagged
        reason = _check_flag_reason(tool_name, gate)
        if reason:
            flagged.append(
                FlaggedStep(
                    index=idx,
                    tool_name=tool_name,
                    arguments=record.request.get("arguments", {}),
                    response=record.response,
                    reason=reason,
                )
            )

    total = len(journal)
    return ExecutionSummary(
        total_steps=total,
        auto_verified=total - len(flagged),
        flagged=flagged,
        tools_used=tools_used,
    )


def _check_flag_reason(tool_name: str, gate: GateProtocol) -> str | None:
    """Return a flag reason if the tool needs review, else None."""
    if not gate.has_tool(tool_name):
        return None  # kernel-internal or unknown — skip

    try:
        meta: ToolMetadata = gate.get_tool_meta(tool_name)
    except Exception:
        return None

    if meta.requires_hitl:
        return "requires HITL approval"
    if meta.destructive:
        return "destructive tool"
    return None
