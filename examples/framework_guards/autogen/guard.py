"""Castor guard layer for AutoGen 0.4+.

Wraps a StaticWorkbench's call_tool method to enforce budget limits
and HITL gates. No agent subclassing needed.

Usage::

    from autogen_agentchat.agents import AssistantAgent
    from autogen_core.tools import FunctionTool, StaticWorkbench

    tools = [
        FunctionTool(web_search, description="Search the web"),
        FunctionTool(delete_files, description="Delete files"),
    ]
    workbench = StaticWorkbench(tools)

    guarded = CastorGuardedWorkbench(
        inner=workbench,
        budgets={"api": 10.0, "disk": 5.0},
        tool_policies={
            "web_search":   {"resource": "api",  "cost": 1.0},
            "delete_files": {"resource": "disk", "cost": 2.0, "destructive": True},
        },
    )

    agent = AssistantAgent(name="assistant", model_client=..., workbench=guarded)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from autogen_core import CancellationToken
from autogen_core.tools import Workbench
from autogen_core.tools._base import ToolResult, ToolSchema

from castor.budget.manager import BudgetManager


class ToolRejectedError(Exception):
    """Raised when a destructive tool call is rejected by the HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool call rejected by human reviewer: {tool_name!r}")


class CastorGuardedWorkbench(Workbench):
    """Wraps an AutoGen Workbench with Castor budget + HITL enforcement.

    Args:
        inner: The original workbench to delegate to.
        budgets: Resource budgets, e.g. ``{"api": 10.0, "disk": 5.0}``.
        tool_policies: Per-tool policy mapping tool name to
            ``{"resource": str, "cost": float, "destructive": bool}``.
        hitl_policy: Optional callable ``(tool_name, args) -> bool`` for
            programmatic HITL decisions. None means interactive prompt.
    """

    def __init__(
        self,
        inner: Workbench,
        budgets: dict[str, float],
        tool_policies: dict[str, dict[str, Any]],
        hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> None:
        self._inner = inner
        self.budget_mgr = BudgetManager()
        self.capabilities = self.budget_mgr.create_budgets(budgets)
        self.tool_policies = tool_policies
        self._hitl_policy = hitl_policy
        self.audit_log: list[dict[str, Any]] = []

    async def list_tools(self) -> list[ToolSchema]:
        return await self._inner.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        cancellation_token: CancellationToken | None = None,
    ) -> ToolResult:
        policy = self.tool_policies.get(name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction
        if resource:
            self.budget_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate
        if policy.get("destructive", False):
            self._hitl_gate(name, arguments)

        # 3. Execute via inner workbench
        result = await self._inner.call_tool(name, arguments, cancellation_token)

        # 4. Audit
        self.audit_log.append(
            {"tool": name, "args": arguments, "cost": cost, "resource": resource}
        )
        return result

    def _hitl_gate(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Check human approval for destructive tool calls."""
        if self._hitl_policy is not None:
            if not self._hitl_policy(tool_name, arguments):
                raise ToolRejectedError(tool_name)
            return
        print("\n--- CASTOR HITL GATE ---")
        print(f"Tool: {tool_name}")
        print(f"Args: {arguments}")
        choice = input("[a]pprove / [r]eject: ").strip().lower()
        if choice != "a":
            raise ToolRejectedError(tool_name)

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
