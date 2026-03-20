"""Level 2 Demo: Finance Trading Agent with Crash Recovery + HITL Suspend/Resume.

Two acts demonstrating Castor's deep integration:
  Act 1 — Crash Recovery: run, crash, resume from checkpoint (0 real LLM calls)
  Act 2 — HITL Suspend/Resume: suspend on destructive trade, approve, continue

Run:  uv run python examples/framework_guards/pydantic_ai/demo_deep.py
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

from examples.framework_guards.pydantic_ai.deep_guard import (
    CastorResilientToolset,
    HITLSuspendError,
    ReplayModel,
)
from examples.framework_guards.pydantic_ai.tools import (
    analyze_risk,
    check_portfolio,
    execute_trade,
    fetch_price,
)

TOOLS = [fetch_price, analyze_risk, execute_trade, check_portfolio]

BUDGETS = {"api_calls": 10.0, "trade_usd": 10_000.0}

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

DIVIDER = "=" * 60


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------


def _research_model() -> FunctionModel:
    """Model: fetch_price → analyze_risk → text summary."""
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
        return ModelResponse(
            parts=[TextPart(content="AAPL looks promising. Risk is MEDIUM.")]
        )

    return FunctionModel(handler)


def _trade_model() -> FunctionModel:
    """Model: fetch_price → execute_trade → text confirmation."""
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
                        tool_name="execute_trade",
                        args={
                            "ticker": "AAPL",
                            "action": "BUY",
                            "amount_usd": 500.0,
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Bought $500 of AAPL at $187.44")])

    return FunctionModel(handler)


def _finish_model() -> FunctionModel:
    """Model: just returns text (used after replay completes the tool calls)."""
    return FunctionModel(
        lambda m, i: ModelResponse(
            parts=[TextPart(content="Trade executed successfully. Portfolio updated.")]
        )
    )


# ---------------------------------------------------------------------------
# Act 1: Crash Recovery
# ---------------------------------------------------------------------------


async def act1_crash_recovery() -> None:
    print(f"\n{DIVIDER}")
    print("ACT 1: Crash Recovery")
    print(DIVIDER)

    # ── Phase 1: Run, then simulate crash ──
    print("\n  Phase 1: Running agent (will 'crash' after recording)...")

    resilient = CastorResilientToolset(
        wrapped=FunctionToolset(TOOLS),
        budgets=BUDGETS.copy(),
        tool_policies=TOOL_POLICIES,
    )
    model = _research_model()
    replay_model = ReplayModel(inner_model=model, journal=resilient.journal)
    agent = Agent(replay_model, toolsets=[resilient])
    result = await agent.run("Analyze AAPL")

    log_len = len(resilient.journal.checkpoint.syscall_log)
    llm_calls_phase1 = replay_model.call_count
    print(f"    Result: {result.output}")
    print(f"    Recorded {log_len} syscalls ({llm_calls_phase1} LLM calls)")
    print("    [CRASH] Simulating process crash...")

    # ── Phase 2: Resume from checkpoint ──
    print("\n  Phase 2: Resuming from checkpoint...")

    checkpoint = resilient.journal.checkpoint.model_copy(deep=True)
    resilient2 = CastorResilientToolset(
        wrapped=FunctionToolset(TOOLS),
        budgets=BUDGETS.copy(),
        tool_policies=TOOL_POLICIES,
        checkpoint=checkpoint,
    )
    finish_model = _finish_model()
    replay_model2 = ReplayModel(inner_model=finish_model, journal=resilient2.journal)
    agent2 = Agent(replay_model2, toolsets=[resilient2])
    result2 = await agent2.run("Analyze AAPL")

    replayed = sum(1 for e in resilient2.audit_log if e.get("replayed"))
    live = sum(1 for e in resilient2.audit_log if e.get("replayed") is False)
    real_llm = replay_model2.call_count

    print(f"    Result: {result2.output}")
    print(f"    Replayed {replayed} tool calls, {live} new tool calls")
    print(f"    Real LLM calls during resume: {real_llm}")
    print("    [OK] Zero wasted API calls during replay!")


# ---------------------------------------------------------------------------
# Act 2: HITL Suspend/Resume
# ---------------------------------------------------------------------------


async def act2_hitl_suspend_resume() -> None:
    print(f"\n{DIVIDER}")
    print("ACT 2: HITL Suspend/Resume")
    print(DIVIDER)

    # ── Phase 1: Run until destructive tool triggers HITL ──
    print("\n  Phase 1: Running agent (will suspend on execute_trade)...")

    resilient = CastorResilientToolset(
        wrapped=FunctionToolset(TOOLS),
        budgets=BUDGETS.copy(),
        tool_policies=TOOL_POLICIES,
    )
    model = _trade_model()
    replay_model = ReplayModel(inner_model=model, journal=resilient.journal)
    agent = Agent(replay_model, toolsets=[resilient])

    try:
        await agent.run("Buy $500 of AAPL")
    except HITLSuspendError as e:
        cp = e.checkpoint
        log_len = len(cp.syscall_log)
        print(f"    [SUSPENDED] Pending: {cp.pending_tool}({cp.pending_args})")
        print(f"    Recorded {log_len} syscalls before suspension")
        print(f"    Suspended: {cp.is_suspended}")

    # ── Phase 2: Human approves, agent resumes ──
    print("\n  Phase 2: Human approves trade, resuming...")

    checkpoint = resilient.journal.checkpoint.model_copy(deep=True)
    approved = checkpoint.pending_hitl
    checkpoint.pending_hitl = None
    checkpoint.status = "RUNNING"

    resilient2 = CastorResilientToolset(
        wrapped=FunctionToolset(TOOLS),
        budgets=BUDGETS.copy(),
        tool_policies=TOOL_POLICIES,
        checkpoint=checkpoint,
        hitl_approved_request=approved,
    )
    finish_model = _finish_model()
    replay_model2 = ReplayModel(inner_model=finish_model, journal=resilient2.journal)
    agent2 = Agent(replay_model2, toolsets=[resilient2])
    result = await agent2.run("Buy $500 of AAPL")

    replayed = [e for e in resilient2.audit_log if e.get("replayed")]
    live = [e for e in resilient2.audit_log if e.get("replayed") is False]
    real_llm = replay_model2.call_count

    print(f"    Result: {result.output}")
    print(f"    Replayed: {[e['tool'] for e in replayed]}")
    print(f"    Live: {[e['tool'] for e in live]}")
    print(f"    Real LLM calls during resume: {real_llm}")

    print("\n  Budget summary:")
    for name, info in resilient2.budget_summary().items():
        print(f"    {name}: {info['used']:.1f} / {info['max']:.1f} used")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    print("Pydantic-AI + Castor Deep Integration Demo")
    print("Finance Trading Agent — Crash Recovery & HITL")
    await act1_crash_recovery()
    await act2_hitl_suspend_resume()
    print(f"\n{DIVIDER}")
    print("Demo complete.")
    print(DIVIDER)


if __name__ == "__main__":
    asyncio.run(main())
