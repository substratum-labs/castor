"""Castor guard layer for Google ADK (Agent Development Kit).

Uses ADK's before_tool_callback to enforce budget limits and HITL gates.
Can be used as an agent-level callback or as a global plugin.

Usage (agent-level callback)::

    from google.adk.agents import Agent

    guard = castor_before_tool_callback(
        budgets={"api": 10.0, "disk": 5.0},
        tool_policies={
            "web_search":   {"resource": "api",  "cost": 1.0},
            "delete_files": {"resource": "disk", "cost": 2.0, "destructive": True},
        },
    )
    agent = Agent(
        name="assistant",
        model="gemini-2.0-flash",
        tools=[web_search, delete_files],
        before_tool_callback=guard,
    )

Usage (global plugin)::

    from google.adk.runners import InMemoryRunner

    plugin = CastorGuardPlugin(
        budgets={"api": 10.0, "disk": 5.0},
        tool_policies={...},
    )
    runner = InMemoryRunner(agent=root_agent, app_name="app", plugins=[plugin])
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import BaseTool
from google.adk.tools.tool_context import ToolContext

from castor.capability.manager import CapabilityManager


class ToolRejectedError(Exception):
    """Raised when a destructive tool call is rejected by the HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool call rejected by human reviewer: {tool_name!r}")


def castor_before_tool_callback(
    budgets: dict[str, float],
    tool_policies: dict[str, dict[str, Any]],
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None,
) -> Callable:
    """Create an ADK before_tool_callback with Castor budget + HITL.

    Args:
        budgets: Resource budgets, e.g. ``{"api": 10.0, "disk": 5.0}``.
        tool_policies: Per-tool policy mapping tool name to
            ``{"resource": str, "cost": float, "destructive": bool}``.
        hitl_policy: Optional callable ``(tool_name, args) -> bool`` for
            programmatic HITL decisions. None means interactive prompt.

    Returns:
        A callback function for ``Agent(before_tool_callback=...)``.
    """
    cap_mgr = CapabilityManager()
    capabilities = cap_mgr.create_capabilities(budgets)
    audit_log: list[dict[str, Any]] = []

    def callback(
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        tool_name = tool.name
        policy = tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction (raises CapabilityExhaustedError if over budget)
        if resource:
            cap_mgr.deduct(capabilities, resource, cost)

        # 2. HITL gate for destructive tools
        if policy.get("destructive", False):
            if not _hitl_check(tool_name, args, hitl_policy):
                return {"status": "rejected", "reason": "Blocked by HITL gate"}

        # 3. Audit
        audit_log.append(
            {"tool": tool_name, "args": args, "cost": cost, "resource": resource}
        )
        return None  # Proceed with execution

    # Attach state for external access
    callback.capabilities = capabilities  # type: ignore[attr-defined]
    callback.audit_log = audit_log  # type: ignore[attr-defined]
    callback.cap_mgr = cap_mgr  # type: ignore[attr-defined]
    return callback


class CastorGuardPlugin(BasePlugin):
    """ADK Plugin that enforces Castor budget + HITL on all tool calls.

    Register on the Runner to guard all agents globally.
    """

    def __init__(
        self,
        budgets: dict[str, float],
        tool_policies: dict[str, dict[str, Any]],
        hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> None:
        super().__init__(name="castor_guard")
        self.cap_mgr = CapabilityManager()
        self.capabilities = self.cap_mgr.create_capabilities(budgets)
        self.tool_policies = tool_policies
        self._hitl_policy = hitl_policy
        self.audit_log: list[dict[str, Any]] = []

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        tool_name = tool.name
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate
        if policy.get("destructive", False):
            if not _hitl_check(tool_name, tool_args, self._hitl_policy):
                return {"status": "rejected", "reason": "Blocked by HITL gate"}

        # 3. Audit
        self.audit_log.append(
            {"tool": tool_name, "args": tool_args, "cost": cost, "resource": resource}
        )
        return None  # Proceed

    def budget_summary(self) -> dict[str, dict[str, float]]:
        """Return current budget usage for display."""
        return {
            name: {
                "used": cap.current_usage,
                "max": cap.max_budget,
                "remaining": cap.max_budget - cap.current_usage,
            }
            for name, cap in self.capabilities.items()
        }


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
