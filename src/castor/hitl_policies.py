"""Built-in HITL policies for use with ``kernel.run_until_complete()``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from castor.models.checkpoint import AgentCheckpoint


async def auto_approve(cp: AgentCheckpoint) -> tuple[str, str | None]:
    """Approve every HITL request automatically."""
    return ("approve", None)


async def auto_reject(cp: AgentCheckpoint) -> tuple[str, str | None]:
    """Reject every HITL request with a generic message."""
    return ("reject", "Automatically rejected by policy")


async def interactive(cp: AgentCheckpoint) -> tuple[str, str | None]:
    """Prompt the user in the terminal for each HITL decision."""
    print(f"\n  HITL: {cp.pending_tool}({cp.pending_args})")
    choice = input("  [a]pprove / [r]eject / [m]odify: ").strip().lower()
    if choice.startswith("r"):
        reason = input("  Reason: ")
        return ("reject", reason)
    if choice.startswith("m"):
        feedback = input("  Feedback: ")
        return ("modify", feedback)
    return ("approve", None)
