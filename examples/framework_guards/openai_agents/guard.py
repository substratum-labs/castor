"""Castor guard layer for OpenAI Agents SDK.

Wraps FunctionTool's on_invoke_tool callable to enforce budget limits
and HITL gates. No agent subclassing needed.

Usage::

    from agents import Agent, FunctionTool, function_tool

    @function_tool
    def web_search(query: str) -> str:
        return f"Results for {query}"

    @function_tool
    def delete_files(paths: list[str]) -> str:
        return f"Deleted {len(paths)} files"

    # Wrap tools with Castor guard
    guarded = guard_tools(
        [web_search, delete_files],
        budgets={"api": 10.0, "disk": 5.0},
        tool_policies={
            "web_search":   {"resource": "api",  "cost": 1.0},
            "delete_files": {"resource": "disk", "cost": 2.0, "destructive": True},
        },
    )

    agent = Agent(name="assistant", tools=guarded, ...)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext

from castor.budget.manager import BudgetManager


class ToolRejectedError(Exception):
    """Raised when a destructive tool call is rejected by the HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool call rejected by human reviewer: {tool_name!r}")


def guard_tools(
    tools: list[FunctionTool],
    budgets: dict[str, float],
    tool_policies: dict[str, dict[str, Any]],
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None,
) -> list[FunctionTool]:
    """Wrap OpenAI Agents SDK tools with Castor budget + HITL enforcement.

    Replaces each tool's on_invoke_tool with a guarded version.

    Args:
        tools: FunctionTool instances to guard.
        budgets: Resource budgets, e.g. ``{"api": 10.0, "disk": 5.0}``.
        tool_policies: Per-tool policy mapping tool name to
            ``{"resource": str, "cost": float, "destructive": bool}``.
        hitl_policy: Optional callable ``(tool_name, args) -> bool`` for
            programmatic HITL decisions. None means interactive prompt.

    Returns:
        The same tool list with on_invoke_tool wrapped in-place.
    """
    budget_mgr = BudgetManager()
    capabilities = budget_mgr.create_budgets(budgets)
    audit_log: list[dict[str, Any]] = []

    for tool in tools:
        original_invoke = tool.on_invoke_tool
        tool_name = tool.name
        policy = tool_policies.get(tool_name, {})

        async def guarded_invoke(
            ctx: ToolContext,
            args_json: str,
            *,
            _original=original_invoke,
            _name=tool_name,
            _policy=policy,
        ) -> Any:
            resource = _policy.get("resource")
            cost = _policy.get("cost", 0.0)
            args = json.loads(args_json) if args_json else {}

            # 1. Budget deduction
            if resource:
                budget_mgr.deduct(capabilities, resource, cost)

            # 2. HITL gate
            if _policy.get("destructive", False):
                _hitl_gate(_name, args, hitl_policy)

            # 3. Execute original
            result = await _original(ctx, args_json)

            # 4. Audit
            audit_log.append(
                {"tool": _name, "args": args, "cost": cost, "resource": resource}
            )
            return result

        tool.on_invoke_tool = guarded_invoke  # type: ignore[assignment]

    # Attach for external access
    guard_tools.capabilities = capabilities  # type: ignore[attr-defined]
    guard_tools.audit_log = audit_log  # type: ignore[attr-defined]
    return tools


def _hitl_gate(
    tool_name: str,
    arguments: dict[str, Any],
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None,
) -> None:
    """Check human approval for destructive tool calls."""
    if hitl_policy is not None:
        if not hitl_policy(tool_name, arguments):
            raise ToolRejectedError(tool_name)
        return
    print("\n--- CASTOR HITL GATE ---")
    print(f"Tool: {tool_name}")
    print(f"Args: {arguments}")
    choice = input("[a]pprove / [r]eject: ").strip().lower()
    if choice != "a":
        raise ToolRejectedError(tool_name)
