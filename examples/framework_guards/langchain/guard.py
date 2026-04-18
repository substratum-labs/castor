"""Castor guard layer for LangChain / LangGraph.

Uses LangGraph's ToolNode wrap_tool_call middleware to intercept all tool
calls with budget enforcement and HITL gates. No agent subclassing needed.

Usage (LangGraph)::

    from langgraph.prebuilt import create_react_agent, ToolNode

    tools = [web_search, delete_files]
    guarded_node = castor_tool_node(
        tools,
        budgets={"api": 10.0, "disk": 5.0},
        tool_policies={
            "web_search":    {"resource": "api",  "cost": 1.0},
            "delete_files":  {"resource": "disk", "cost": 2.0, "destructive": True},
        },
    )
    # Use guarded_node in your LangGraph workflow

Usage (BaseTool wrapping, works with any LangChain agent)::

    from langchain_core.tools import BaseTool
    tools = guard_tools(
        [web_search_tool, delete_files_tool],
        budgets={"api": 10.0, "disk": 5.0},
        tool_policies={...},
    )
    agent = AgentExecutor(agent=llm_agent, tools=tools)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode

from castor.budget.manager import BudgetManager


class ToolRejectedError(Exception):
    """Raised when a destructive tool call is rejected by the HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool call rejected by human reviewer: {tool_name!r}")


def castor_tool_node(
    tools: list[BaseTool],
    budgets: dict[str, float],
    tool_policies: dict[str, dict[str, Any]],
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None,
) -> ToolNode:
    """Create a LangGraph ToolNode with Castor budget + HITL enforcement.

    Args:
        tools: LangChain tools to guard.
        budgets: Resource budgets, e.g. ``{"api": 10.0, "disk": 5.0}``.
        tool_policies: Per-tool policy mapping tool name to
            ``{"resource": str, "cost": float, "destructive": bool}``.
        hitl_policy: Optional callable ``(tool_name, args) -> bool`` for
            programmatic HITL decisions. None means interactive prompt.
    """
    budget_mgr = BudgetManager()
    capabilities = budget_mgr.create_budgets(budgets)
    audit_log: list[dict[str, Any]] = []

    async def castor_guard(
        request: ToolCallRequest,
        execute: Callable,
    ) -> ToolMessage:
        tool_name = request.tool_call["name"]
        tool_args = request.tool_call.get("args", {})
        policy = tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction
        if resource:
            budget_mgr.deduct(capabilities, resource, cost)

        # 2. HITL gate
        if policy.get("destructive", False):
            _hitl_gate(tool_name, tool_args, hitl_policy)

        # 3. Execute
        result = await execute(request)

        # 4. Audit
        audit_log.append(
            {"tool": tool_name, "args": tool_args, "cost": cost, "resource": resource}
        )
        return result

    node = ToolNode(tools=tools, awrap_tool_call=castor_guard)
    # Attach for external access
    node.castor_capabilities = capabilities  # type: ignore[attr-defined]
    node.castor_audit_log = audit_log  # type: ignore[attr-defined]
    return node


def guard_tools(
    tools: list[BaseTool],
    budgets: dict[str, float],
    tool_policies: dict[str, dict[str, Any]],
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None,
) -> list[CastorGuardedTool]:
    """Wrap LangChain tools with Castor budget + HITL (for AgentExecutor).

    Returns a list of guarded tools that can replace the originals.
    """
    budget_mgr = BudgetManager()
    capabilities = budget_mgr.create_budgets(budgets)
    return [
        CastorGuardedTool(
            inner=t,
            budget_mgr=budget_mgr,
            capabilities=capabilities,
            policy=tool_policies.get(t.name, {}),
            hitl_policy=hitl_policy,
        )
        for t in tools
    ]


class CastorGuardedTool(BaseTool):
    """Wraps a LangChain BaseTool with Castor budget + HITL enforcement."""

    inner: BaseTool
    budget_mgr: BudgetManager
    capabilities: dict[str, Any]
    policy: dict[str, Any]
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.inner.name

    @property
    def description(self) -> str:  # type: ignore[override]
        return self.inner.description

    @property
    def args_schema(self):  # type: ignore[override]
        return self.inner.args_schema

    def _run(self, **kwargs: Any) -> Any:
        resource = self.policy.get("resource")
        cost = self.policy.get("cost", 0.0)

        if resource:
            self.budget_mgr.deduct(self.capabilities, resource, cost)
        if self.policy.get("destructive", False):
            _hitl_gate(self.name, kwargs, self.hitl_policy)

        return self.inner._run(**kwargs)

    async def _arun(self, **kwargs: Any) -> Any:
        resource = self.policy.get("resource")
        cost = self.policy.get("cost", 0.0)

        if resource:
            self.budget_mgr.deduct(self.capabilities, resource, cost)
        if self.policy.get("destructive", False):
            _hitl_gate(self.name, kwargs, self.hitl_policy)

        return await self.inner._arun(**kwargs)


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
