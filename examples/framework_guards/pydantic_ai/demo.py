"""Level 1 Demo: Finance Trading Agent with Castor Guard Layer.

Three acts demonstrating progressive security:
  Act 1 — Vanilla pydantic-ai agent (no protection)
  Act 2 — CastorGuardedToolset (budget + HITL)
  Act 3 — Budget exhaustion (hard cap enforcement)

Run:  uv run python examples/framework_guards/pydantic_ai/demo.py
"""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets.function import FunctionToolset

from castor.budget.manager import BudgetExhaustedError
from examples.framework_guards.pydantic_ai.guard import (
    CastorGuardedToolset,
    ToolRejectedError,
)
from examples.framework_guards.pydantic_ai.tools import (
    analyze_risk,
    check_portfolio,
    execute_trade,
    fetch_price,
)

TOOLS = [fetch_price, analyze_risk, execute_trade, check_portfolio]

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


# ---------------------------------------------------------------------------
# Fake model that simulates a trading agent's tool-calling behaviour
# ---------------------------------------------------------------------------


def _trading_model() -> FunctionModel:
    """Model that: fetch_price → analyze_risk → execute_trade → done."""
    step = 0

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal step
        step += 1
        if step == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="fetch_price", args={"ticker": "AAPL"})]
            )
        if step == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="analyze_risk",
                        args={"ticker": "AAPL", "position_usd": 2000.0},
                    )
                ]
            )
        if step == 3:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="execute_trade",
                        args={
                            "ticker": "AAPL",
                            "action": "BUY",
                            "amount_usd": 500.0,
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Trade analysis complete.")])

    return FunctionModel(handler)


def _greedy_model() -> FunctionModel:
    """Model that calls fetch_price repeatedly until budget runs out."""
    tickers = ["AAPL", "GOOGL", "TSLA", "MSFT", "AMZN"]
    step = 0

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal step
        if step < len(tickers):
            ticker = tickers[step]
            step += 1
            return ModelResponse(
                parts=[ToolCallPart(tool_name="fetch_price", args={"ticker": ticker})]
            )
        return ModelResponse(parts=[TextPart(content="Done")])

    return FunctionModel(handler)


# ---------------------------------------------------------------------------
# Acts
# ---------------------------------------------------------------------------

DIVIDER = "=" * 60


async def act1_vanilla() -> None:
    """Act 1: Vanilla pydantic-ai — no protection."""
    print(f"\n{DIVIDER}")
    print("ACT 1: Vanilla pydantic-ai Agent (no protection)")
    print(DIVIDER)

    model = _trading_model()
    agent = Agent(model, toolsets=[FunctionToolset(TOOLS)])
    result = await agent.run("Analyze AAPL and buy if promising")

    print(f"  Result: {result.output}")
    print("  [!] No budget tracking. No HITL approval. Trade executed freely.")


async def act2_guarded() -> None:
    """Act 2: CastorGuardedToolset — budget + HITL."""
    print(f"\n{DIVIDER}")
    print("ACT 2: Castor Guarded Agent (budget + HITL)")
    print(DIVIDER)

    model = _trading_model()
    guarded = CastorGuardedToolset(
        wrapped=FunctionToolset(TOOLS),
        budgets=BUDGETS.copy(),
        tool_policies=TOOL_POLICIES,
        hitl_policy=lambda name, args: False,  # reject all destructive calls
    )
    agent = Agent(model, toolsets=[guarded])

    try:
        result = await agent.run("Analyze AAPL and buy if promising")
        print(f"  Result: {result.output}")
    except ToolRejectedError as e:
        print(f"  [BLOCKED] {e}")

    print("\n  Budget summary:")
    for name, info in guarded.budget_summary().items():
        print(f"    {name}: {info['used']:.1f} / {info['max']:.1f} used")

    print(f"\n  Audit log ({len(guarded.audit_log)} entries):")
    for entry in guarded.audit_log:
        print(f"    - {entry['tool']} (cost={entry['cost']})")


async def act3_exhaustion() -> None:
    """Act 3: Budget exhaustion — hard cap."""
    print(f"\n{DIVIDER}")
    print("ACT 3: Budget Exhaustion (hard cap)")
    print(DIVIDER)

    model = _greedy_model()
    guarded = CastorGuardedToolset(
        wrapped=FunctionToolset(TOOLS),
        budgets={"api_calls": 3.0},
        tool_policies={
            "fetch_price": {"resource": "api_calls", "cost": 1.0},
        },
    )
    agent = Agent(model, toolsets=[guarded])

    try:
        result = await agent.run("Get prices for all tech stocks")
        print(f"  Result: {result.output}")
    except BudgetExhaustedError as e:
        print(f"  [EXHAUSTED] {e}")

    print("\n  Budget summary:")
    for name, info in guarded.budget_summary().items():
        print(f"    {name}: {info['used']:.1f} / {info['max']:.1f} used")

    print(f"  Calls completed before exhaustion: {len(guarded.audit_log)}")


async def main() -> None:
    print("Pydantic-AI + Castor Guard Layer Demo")
    print("Finance Trading Agent — Three Acts")
    await act1_vanilla()
    await act2_guarded()
    await act3_exhaustion()
    print(f"\n{DIVIDER}")
    print("Demo complete.")
    print(DIVIDER)


if __name__ == "__main__":
    asyncio.run(main())
