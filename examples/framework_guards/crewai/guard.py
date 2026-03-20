"""Castor guard layer for CrewAI.

Uses CrewAI's before_tool_call / after_tool_call hook system to enforce
budget limits and HITL gates. No subclassing needed.

Usage::

    from crewai import Agent, Task, Crew
    from crewai.hooks import register_before_tool_call_hook

    # Register Castor guard globally
    guard = castor_guard_hook(
        budgets={"api": 10.0, "disk": 5.0},
        tool_policies={
            "web_search":   {"resource": "api",  "cost": 1.0},
            "delete_files": {"resource": "disk", "cost": 2.0, "destructive": True},
        },
    )
    register_before_tool_call_hook(guard)

    # Build crew as normal — all tool calls go through Castor
    agent = Agent(role="researcher", tools=[web_search, delete_files], ...)
    crew = Crew(agents=[agent], tasks=[...])
    crew.kickoff()
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from castor.capability.manager import CapabilityManager


class ToolRejectedError(Exception):
    """Raised when a destructive tool call is rejected by the HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool call rejected by human reviewer: {tool_name!r}")


def castor_guard_hook(
    budgets: dict[str, float],
    tool_policies: dict[str, dict[str, Any]],
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None,
) -> Callable:
    """Create a CrewAI before_tool_call hook with Castor budget + HITL.

    Args:
        budgets: Resource budgets, e.g. ``{"api": 10.0, "disk": 5.0}``.
        tool_policies: Per-tool policy mapping tool name to
            ``{"resource": str, "cost": float, "destructive": bool}``.
        hitl_policy: Optional callable ``(tool_name, args) -> bool`` for
            programmatic HITL decisions. None means interactive prompt.

    Returns:
        A hook function to pass to ``register_before_tool_call_hook()``.
    """
    cap_mgr = CapabilityManager()
    capabilities = cap_mgr.create_capabilities(budgets)
    audit_log: list[dict[str, Any]] = []

    def guard(context) -> bool | None:
        """CrewAI before_tool_call hook. Return False to block execution."""
        tool_name = context.tool_name
        tool_input = context.tool_input
        policy = tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction (raises CapabilityExhaustedError if over budget)
        if resource:
            cap_mgr.deduct(capabilities, resource, cost)

        # 2. HITL gate for destructive tools
        if policy.get("destructive", False):
            if not _hitl_check(tool_name, tool_input, hitl_policy):
                return False  # Block execution

        # 3. Audit
        audit_log.append(
            {"tool": tool_name, "args": tool_input, "cost": cost, "resource": resource}
        )
        return None  # Proceed with execution

    # Attach state for external access
    guard.capabilities = capabilities  # type: ignore[attr-defined]
    guard.audit_log = audit_log  # type: ignore[attr-defined]
    guard.cap_mgr = cap_mgr  # type: ignore[attr-defined]
    return guard


def _hitl_check(
    tool_name: str,
    arguments: dict[str, Any],
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None,
) -> bool:
    """Check human approval for destructive tool calls. Returns True if approved."""
    if hitl_policy is not None:
        return hitl_policy(tool_name, arguments)
    print("\n--- CASTOR HITL GATE ---")
    print(f"Tool: {tool_name}")
    print(f"Args: {arguments}")
    choice = input("[a]pprove / [r]eject: ").strip().lower()
    return choice == "a"
