"""Level 1: CastorGuardedToolset — budget enforcement + HITL gates.

Wraps any pydantic-ai toolset with Castor capability budgets and
human-in-the-loop approval gates. Zero changes to the Agent class;
all security lives in the toolset layer.

Usage:
    from pydantic_ai.guard import CastorGuardedToolset

    inner = FunctionToolset([fetch_price, execute_trade])
    guarded = CastorGuardedToolset(
        wrapped=inner,
        budgets={"api_calls": 5.0, "trade_usd": 10_000.0},
        tool_policies={
            "fetch_price":    {"resource": "api_calls", "cost": 1.0},
            "execute_trade": {
                "resource": "trade_usd",
                "cost": 500.0,
                "destructive": True,
            },
        },
    )
    agent = Agent("openai:gpt-4o", toolsets=[guarded])
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai._run_context import RunContext
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from castor.capability.manager import CapabilityManager
from castor.models.capability import Capability


class ToolRejectedError(Exception):
    """Raised when a destructive tool call is rejected by the HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool call rejected by human reviewer: {tool_name!r}")


@dataclass
class CastorGuardedToolset(WrapperToolset[Any]):
    """Wraps a pydantic-ai toolset with Castor budget + HITL enforcement.

    - Budget: each tool call deducts from a named capability budget.
    - HITL: destructive tools require human approval before execution.
    - Audit: every call (success or failure) is logged.
    """

    budgets: dict[str, float] = field(default_factory=dict)
    tool_policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    hitl_policy: Callable[[str, dict[str, Any]], bool] | None = None

    # Computed fields — not passed by caller
    cap_mgr: CapabilityManager = field(init=False, repr=False)
    capabilities: dict[str, Capability] = field(init=False, repr=False)
    audit_log: list[dict[str, Any]] = field(
        init=False, default_factory=list, repr=False
    )

    def __post_init__(self) -> None:
        self.cap_mgr = CapabilityManager()
        self.capabilities = self.cap_mgr.create_capabilities(self.budgets)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        policy = self.tool_policies.get(name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction — hard cap (raises CapabilityExhaustedError)
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate — destructive tools require approval
        if policy.get("destructive", False):
            self._hitl_gate(name, tool_args)

        # 3. Execute via wrapped toolset (pydantic-ai handles retries/validation)
        result = await super().call_tool(name, tool_args, ctx, tool)

        # 4. Audit
        self.audit_log.append(
            {"tool": name, "args": tool_args, "cost": cost, "resource": resource}
        )
        return result

    def _hitl_gate(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Check human approval for destructive tool calls."""
        if self.hitl_policy is not None:
            if not self.hitl_policy(tool_name, arguments):
                raise ToolRejectedError(tool_name)
            return

        # Interactive mode (CLI)
        print("\n--- CASTOR HITL GATE ---")
        print(f"Tool: {tool_name}")
        print(f"Args: {arguments}")
        choice = input("[a]pprove / [r]eject: ").strip().lower()
        if choice != "a":
            raise ToolRejectedError(tool_name)

    def visit_and_replace(
        self,
        visitor: Callable[[AbstractToolset[Any]], AbstractToolset[Any]],
    ) -> AbstractToolset[Any]:
        """Preserve mutable state (capabilities, audit_log) across copies.

        pydantic-ai calls visit_and_replace() during agent setup, which uses
        dataclasses.replace() and triggers __post_init__ on the copy.  We
        override to share our mutable objects so budget/audit tracking works
        on the instance the agent actually uses.
        """
        new_wrapped = self.wrapped.visit_and_replace(visitor)
        result = replace(self, wrapped=new_wrapped)
        result.cap_mgr = self.cap_mgr
        result.capabilities = self.capabilities
        result.audit_log = self.audit_log
        return result

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
