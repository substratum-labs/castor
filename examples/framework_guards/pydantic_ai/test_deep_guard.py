"""Level 2 tests: CastorResilientToolset checkpoint/replay + HITL suspend/resume."""

from __future__ import annotations

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets.function import FunctionToolset

from castor.models.checkpoint import AgentCheckpoint
from examples.framework_guards.pydantic_ai.deep_guard import (
    CastorResilientToolset,
    HITLSuspendError,
    ReplayModel,
    _deserialize_response,
    _serialize_response,
)
from examples.framework_guards.pydantic_ai.tools import (
    analyze_risk,
    check_portfolio,
    execute_trade,
    fetch_price,
)

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

BUDGETS = {"api_calls": 5.0, "trade_usd": 10_000.0}

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


def _make_resilient(
    *,
    checkpoint: AgentCheckpoint | None = None,
    hitl_approved_request: dict | None = None,
) -> CastorResilientToolset:
    inner = FunctionToolset(TOOLS)
    return CastorResilientToolset(
        wrapped=inner,
        budgets=BUDGETS.copy(),
        tool_policies=TOOL_POLICIES,
        checkpoint=checkpoint,
        hitl_approved_request=hitl_approved_request,
    )


def _fresh_checkpoint(toolset: CastorResilientToolset) -> AgentCheckpoint:
    """Deep-copy the toolset's checkpoint for use in a resumed agent."""
    return toolset.journal.checkpoint.model_copy(deep=True)


# ---------------------------------------------------------------------------
# Helper models
# ---------------------------------------------------------------------------


def _model_call_then_text(tool_name: str, tool_args: dict) -> FunctionModel:
    """Model that calls one tool, then returns text."""
    calls = 0

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name=tool_name, args=tool_args)]
            )
        return ModelResponse(parts=[TextPart(content="Analysis complete")])

    return FunctionModel(handler)


def _model_two_tools_then_text(
    tool1: str, args1: dict, tool2: str, args2: dict
) -> FunctionModel:
    """Model that calls two tools sequentially, then returns text."""
    calls = 0

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name=tool1, args=args1)])
        if calls == 2:
            return ModelResponse(parts=[ToolCallPart(tool_name=tool2, args=args2)])
        return ModelResponse(parts=[TextPart(content="Done")])

    return FunctionModel(handler)


# ---------------------------------------------------------------------------
# Tests: serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_model_response_roundtrip(self):
        """ModelResponse -> dict -> ModelResponse preserves content."""
        original = ModelResponse(parts=[TextPart(content="hello world")])
        data = _serialize_response(original)
        restored = _deserialize_response(data)
        assert len(restored.parts) == 1
        assert restored.parts[0].content == "hello world"

    def test_tool_call_roundtrip(self):
        """ToolCallPart survives serialization roundtrip."""
        original = ModelResponse(
            parts=[ToolCallPart(tool_name="fetch_price", args={"ticker": "AAPL"})]
        )
        data = _serialize_response(original)
        restored = _deserialize_response(data)
        assert len(restored.parts) == 1
        part = restored.parts[0]
        assert isinstance(part, ToolCallPart)
        assert part.tool_name == "fetch_price"
        assert part.args == {"ticker": "AAPL"}


# ---------------------------------------------------------------------------
# Tests: recording
# ---------------------------------------------------------------------------


class TestRecording:
    async def test_tool_results_recorded(self):
        """Tool calls should be recorded in the syscall log."""
        resilient = _make_resilient()
        inner_model = _model_call_then_text("fetch_price", {"ticker": "AAPL"})
        replay_model = ReplayModel(inner_model=inner_model, journal=resilient.journal)
        agent = Agent(replay_model, toolsets=[resilient])

        await agent.run("Check AAPL")

        # Should have LLM call + tool call + LLM call in syscall_log
        log = resilient.journal.checkpoint.syscall_log
        tool_records = [r for r in log if r.request.get("tool_name") != "__llm__"]
        assert len(tool_records) == 1
        assert tool_records[0].request["tool_name"] == "fetch_price"

    async def test_llm_calls_recorded(self):
        """LLM calls should be recorded with tool_name='__llm__'."""
        resilient = _make_resilient()
        inner_model = _model_call_then_text("fetch_price", {"ticker": "AAPL"})
        replay_model = ReplayModel(inner_model=inner_model, journal=resilient.journal)
        agent = Agent(replay_model, toolsets=[resilient])

        await agent.run("Check AAPL")

        log = resilient.journal.checkpoint.syscall_log
        llm_records = [r for r in log if r.request.get("tool_name") == "__llm__"]
        assert len(llm_records) >= 1
        assert replay_model.call_count >= 1


# ---------------------------------------------------------------------------
# Tests: replay
# ---------------------------------------------------------------------------


class TestReplay:
    async def test_replay_serves_cached_tools(self):
        """Resumed agent should serve cached tool results without executing."""
        # Phase 1: record
        resilient1 = _make_resilient()
        inner_model1 = _model_call_then_text("fetch_price", {"ticker": "AAPL"})
        replay_model1 = ReplayModel(
            inner_model=inner_model1, journal=resilient1.journal
        )
        agent1 = Agent(replay_model1, toolsets=[resilient1])
        await agent1.run("Check AAPL")

        recorded_log_len = len(resilient1.journal.checkpoint.syscall_log)
        assert recorded_log_len > 0

        # Phase 2: replay from checkpoint
        checkpoint = _fresh_checkpoint(resilient1)
        resilient2 = _make_resilient(checkpoint=checkpoint)
        inner_model2 = _model_call_then_text("fetch_price", {"ticker": "AAPL"})
        replay_model2 = ReplayModel(
            inner_model=inner_model2, journal=resilient2.journal
        )
        agent2 = Agent(replay_model2, toolsets=[resilient2])
        await agent2.run("Check AAPL")

        # Inner model should NOT have been called during replay portion
        # (it may be called for the final response if replay completes early)
        assert any(entry.get("replayed") for entry in resilient2.audit_log), (
            "Expected at least one replayed tool call"
        )

    async def test_replay_serves_cached_llm(self):
        """ReplayModel should serve cached LLM responses during replay."""
        # Phase 1: record
        resilient1 = _make_resilient()
        inner_model1 = _model_call_then_text("fetch_price", {"ticker": "AAPL"})
        replay_model1 = ReplayModel(
            inner_model=inner_model1, journal=resilient1.journal
        )
        agent1 = Agent(replay_model1, toolsets=[resilient1])
        await agent1.run("Check AAPL")

        original_call_count = replay_model1.call_count
        assert original_call_count >= 1

        # Phase 2: replay — inner model should not be called during replay
        checkpoint = _fresh_checkpoint(resilient1)
        resilient2 = _make_resilient(checkpoint=checkpoint)
        inner_model2 = _model_call_then_text("fetch_price", {"ticker": "AAPL"})
        replay_model2 = ReplayModel(
            inner_model=inner_model2, journal=resilient2.journal
        )
        agent2 = Agent(replay_model2, toolsets=[resilient2])
        await agent2.run("Check AAPL")

        # During replay, the inner model call count should be less than original
        assert replay_model2.call_count < original_call_count


# ---------------------------------------------------------------------------
# Tests: HITL suspend/resume
# ---------------------------------------------------------------------------


class TestHITLSuspendResume:
    async def test_hitl_suspends_and_saves(self):
        """Destructive tool should trigger suspension with checkpoint."""
        resilient = _make_resilient()
        inner_model = _model_call_then_text(
            "execute_trade",
            {"ticker": "AAPL", "action": "BUY", "amount_usd": 500.0},
        )
        replay_model = ReplayModel(inner_model=inner_model, journal=resilient.journal)
        agent = Agent(replay_model, toolsets=[resilient])

        with pytest.raises(HITLSuspendError) as exc_info:
            await agent.run("Buy AAPL")

        cp = exc_info.value.checkpoint
        assert cp.status == "SUSPENDED_FOR_HITL"
        assert cp.pending_hitl is not None
        assert cp.pending_hitl["tool_name"] == "execute_trade"

    async def test_hitl_resume_replays_then_continues(self):
        """After HITL approval, agent should replay cached calls then execute."""
        # Phase 1: record safe tool, then hit HITL on destructive tool
        resilient1 = _make_resilient()
        inner_model1 = _model_two_tools_then_text(
            "fetch_price",
            {"ticker": "AAPL"},
            "execute_trade",
            {"ticker": "AAPL", "action": "BUY", "amount_usd": 500.0},
        )
        replay_model1 = ReplayModel(
            inner_model=inner_model1, journal=resilient1.journal
        )
        agent1 = Agent(replay_model1, toolsets=[resilient1])

        with pytest.raises(HITLSuspendError):
            await agent1.run("Buy AAPL")

        # Verify safe tool was recorded, but not the destructive one
        log = resilient1.journal.checkpoint.syscall_log
        tool_records = [r for r in log if r.request.get("tool_name") != "__llm__"]
        assert len(tool_records) == 1  # only fetch_price recorded
        assert tool_records[0].request["tool_name"] == "fetch_price"

        # Phase 2: approve and resume
        checkpoint = _fresh_checkpoint(resilient1)
        approved_request = checkpoint.pending_hitl
        checkpoint.pending_hitl = None
        checkpoint.status = "RUNNING"

        resilient2 = _make_resilient(
            checkpoint=checkpoint,
            hitl_approved_request=approved_request,
        )
        # Phase 2 model: ReplayModel serves cached LLM calls during replay.
        # After replay + live execute_trade, next LLM call returns text to finish.
        inner_model2 = FunctionModel(
            lambda m, i: ModelResponse(parts=[TextPart(content="Trade complete")])
        )
        replay_model2 = ReplayModel(
            inner_model=inner_model2, journal=resilient2.journal
        )
        agent2 = Agent(replay_model2, toolsets=[resilient2])
        result = await agent2.run("Buy AAPL")

        assert result.output is not None

        # Verify: fetch_price was replayed, execute_trade was live
        replayed = [e for e in resilient2.audit_log if e.get("replayed")]
        live = [e for e in resilient2.audit_log if e.get("replayed") is False]
        assert len(replayed) >= 1, "fetch_price should have been replayed"
        assert any(e["tool"] == "execute_trade" for e in live), (
            "execute_trade should have run live"
        )
