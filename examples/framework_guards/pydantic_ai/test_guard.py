"""Level 1 tests: CastorGuardedToolset budget + HITL enforcement."""

from __future__ import annotations

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from castor.capability.manager import CapabilityExhaustedError
from examples.framework_guards.pydantic_ai.guard import CastorGuardedToolset, ToolRejectedError
from examples.framework_guards.pydantic_ai.tools import (
    analyze_risk,
    check_portfolio,
    execute_trade,
    fetch_price,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

BUDGETS = {"api_calls": 3.0, "trade_usd": 10_000.0}

TOOL_POLICIES = {
    "fetch_price": {"resource": "api_calls", "cost": 1.0},
    "analyze_risk": {"resource": "api_calls", "cost": 1.0},
    "execute_trade": {
        "resource": "trade_usd",
        "cost": 500.0,
        "destructive": True,
    },
    "check_portfolio": {"resource": "api_calls", "cost": 1.0},
}

TOOLS = [fetch_price, analyze_risk, execute_trade, check_portfolio]


def _make_guarded(*, hitl_policy=None) -> CastorGuardedToolset:
    """Create a CastorGuardedToolset wrapping the trading tools."""
    from pydantic_ai.toolsets.function import FunctionToolset

    inner = FunctionToolset(TOOLS)
    return CastorGuardedToolset(
        wrapped=inner,
        budgets=BUDGETS.copy(),
        tool_policies=TOOL_POLICIES,
        hitl_policy=hitl_policy,
    )


# ---------------------------------------------------------------------------
# Helper: model that calls one tool then returns text
# ---------------------------------------------------------------------------


def _model_that_calls(tool_name: str, tool_args: dict) -> FunctionModel:
    """Return a FunctionModel that calls a single tool, then returns text."""
    call_count = 0

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name=tool_name, args=tool_args)]
            )
        # After tool return, produce final output
        return ModelResponse(parts=[TextPart(content="Done")])

    return FunctionModel(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBudgetDeduction:
    async def test_budget_deducted_on_tool_call(self):
        """Budget should decrease after a successful tool call."""
        guarded = _make_guarded()
        model = _model_that_calls("fetch_price", {"ticker": "AAPL"})
        agent = Agent(model, toolsets=[guarded])
        await agent.run("What is AAPL's price?")

        summary = guarded.budget_summary()
        assert summary["api_calls"]["used"] == 1.0
        assert summary["api_calls"]["remaining"] == 2.0
        assert len(guarded.audit_log) == 1
        assert guarded.audit_log[0]["tool"] == "fetch_price"

    async def test_budget_exhaustion_blocks_tool(self):
        """Once budget is exhausted, further calls should raise."""
        guarded = _make_guarded()
        # Exhaust api_calls budget (max=3)
        guarded.cap_mgr.deduct(guarded.capabilities, "api_calls", 3.0)

        model = _model_that_calls("fetch_price", {"ticker": "AAPL"})
        agent = Agent(model, toolsets=[guarded])

        with pytest.raises(CapabilityExhaustedError):
            await agent.run("What is AAPL's price?")

        # Tool should NOT have executed
        assert len(guarded.audit_log) == 0


class TestHITLGate:
    async def test_hitl_rejection_blocks_tool(self):
        """Destructive tool should be blocked when HITL rejects."""
        guarded = _make_guarded(hitl_policy=lambda name, args: False)
        model = _model_that_calls(
            "execute_trade",
            {"ticker": "AAPL", "action": "BUY", "amount_usd": 500.0},
        )
        agent = Agent(model, toolsets=[guarded])

        with pytest.raises(ToolRejectedError):
            await agent.run("Buy AAPL")

        # Budget was deducted (before HITL check), but tool didn't execute
        summary = guarded.budget_summary()
        assert summary["trade_usd"]["used"] == 500.0
        assert len(guarded.audit_log) == 0

    async def test_hitl_approval_allows_execution(self):
        """Destructive tool should execute when HITL approves."""
        guarded = _make_guarded(hitl_policy=lambda name, args: True)
        model = _model_that_calls(
            "execute_trade",
            {"ticker": "AAPL", "action": "BUY", "amount_usd": 500.0},
        )
        agent = Agent(model, toolsets=[guarded])
        result = await agent.run("Buy AAPL")

        assert result.output is not None
        summary = guarded.budget_summary()
        assert summary["trade_usd"]["used"] == 500.0
        assert len(guarded.audit_log) == 1
        assert guarded.audit_log[0]["tool"] == "execute_trade"
