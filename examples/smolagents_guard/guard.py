"""CastorGuardedAgent — smolagents + Castor security guard layer.

Subclasses smolagents ToolCallingAgent to add:
- Budget enforcement via Castor CapabilityManager
- HITL gates for destructive tool calls
- Audit logging of all tool executions
"""

from __future__ import annotations

from typing import Any

from smolagents import ToolCallingAgent

from castor.capability.manager import CapabilityManager


class CastorGuardedAgent(ToolCallingAgent):
    """A smolagents ToolCallingAgent with Castor security guardrails.

    Args:
        tools: List of smolagents Tool instances.
        model: The LLM model to use.
        budgets: Resource budgets, e.g. ``{"network": 20.0, "disk": 10.0}``.
        tool_policies: Per-tool policy dict mapping tool name to
            ``{"resource": str, "cost": float, "destructive": bool}``.
        hitl_policy: Optional callable ``(tool_name, arguments) -> bool`` for
            programmatic HITL decisions. ``None`` means interactive prompt.
        **kwargs: Passed to ``ToolCallingAgent.__init__``.
    """

    def __init__(
        self,
        tools,
        model,
        budgets: dict[str, float],
        tool_policies: dict[str, dict[str, Any]],
        hitl_policy=None,
        **kwargs,
    ):
        super().__init__(tools=tools, model=model, **kwargs)
        self.cap_mgr = CapabilityManager()
        self.capabilities = self.cap_mgr.create_capabilities(budgets)
        self.tool_policies = tool_policies
        self._hitl_policy = hitl_policy
        self.audit_log: list[dict[str, Any]] = []

    def execute_tool_call(self, tool_name: str, arguments: dict[str, str] | str) -> Any:
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction — hard cap
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate — destructive tools require human approval
        if policy.get("destructive", False):
            self._hitl_gate(tool_name, arguments)

        # 3. Execute via smolagents original path
        result = super().execute_tool_call(tool_name, arguments)

        # 4. Audit
        self.audit_log.append({
            "tool": tool_name,
            "cost": cost,
            "resource": resource,
        })
        return result

    def _hitl_gate(self, tool_name: str, arguments: dict[str, str] | str) -> None:
        if self._hitl_policy is not None:
            if not self._hitl_policy(tool_name, arguments):
                raise ToolRejectedError(tool_name)
            return
        # Interactive mode
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


class ToolRejectedError(Exception):
    """Raised when a human rejects a destructive tool call via HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' rejected by human via HITL gate")
