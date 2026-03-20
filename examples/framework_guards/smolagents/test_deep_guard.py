"""Tests for CastorResilientAgent -- checkpoint/replay and HITL suspend/resume."""

from __future__ import annotations

import pytest
from smolagents import tool
from smolagents.models import ChatMessage, Model

from .deep_guard import (
    CastorResilientAgent,
    HITLSuspendError,
    _deserialize_chat_message,
    _serialize_chat_message,
)

# -- Stub tools --


@tool
def safe_tool(query: str) -> str:
    """A safe tool that costs 1.0 network.

    Args:
        query: The input query.
    """
    return f"result for {query}"


@tool
def destructive_tool(target: str) -> str:
    """A destructive tool that requires HITL approval.

    Args:
        target: The target to act on.
    """
    return f"destroyed {target}"


POLICIES = {
    "safe_tool": {"resource": "network", "cost": 1.0},
    "destructive_tool": {"resource": "network", "cost": 2.0, "destructive": True},
}

BUDGETS = {"network": 20.0}


# -- Tracking model --


class TrackingModel(Model):
    """Model that tracks call count and returns scripted responses."""

    def __init__(self, responses: list[str]):
        super().__init__(model_id="tracking")
        self._responses = responses
        self._call_index = 0
        self.call_count = 0

    def generate(self, messages, **kwargs):
        self.call_count += 1
        idx = self._call_index
        self._call_index += 1
        content = self._responses[idx] if idx < len(self._responses) else "fallback"
        return ChatMessage(role="assistant", content=content)


def _fresh_checkpoint(checkpoint):
    """Deep-copy a checkpoint so capabilities are not shared references.

    When replaying from a saved checkpoint, the real system would deserialize
    from storage (SQLite), producing fresh Capability objects. In tests we
    simulate this by round-tripping through Pydantic serialization.
    """
    return checkpoint.model_copy(deep=True)


def make_agent(model=None, checkpoint=None, budgets=None, hitl_approved_request=None):
    if model is None:
        model = TrackingModel(["response 1", "response 2", "response 3"])
    return CastorResilientAgent(
        tools=[safe_tool, destructive_tool],
        model=model,
        budgets=budgets or BUDGETS,
        tool_policies=POLICIES,
        checkpoint=checkpoint,
        hitl_approved_request=hitl_approved_request,
    )


# -- Test: ChatMessage serialization round-trip --


def test_chat_message_serialization_roundtrip():
    original = ChatMessage(role="assistant", content="hello world")
    serialized = _serialize_chat_message(original)
    restored = _deserialize_chat_message(serialized)
    assert restored.role == original.role
    assert restored.content == original.content


# -- Test: recording --


def test_tool_results_recorded():
    agent = make_agent()
    agent.execute_tool_call("safe_tool", {"query": "test"})
    assert len(agent._checkpoint.syscall_log) == 1
    record = agent._checkpoint.syscall_log[0]
    assert record.request == {"tool_name": "safe_tool", "arguments": {"query": "test"}}
    assert "result for test" in record.response


def test_llm_calls_recorded():
    agent = make_agent()
    agent.model.generate([{"role": "user", "content": "hi"}])
    assert len(agent._checkpoint.syscall_log) == 1
    record = agent._checkpoint.syscall_log[0]
    assert record.request["tool_name"] == "__llm__"


# -- Test: replay --


def test_replay_serves_cached_tools():
    # Phase 1: record
    model1 = TrackingModel(["resp1"])
    agent1 = make_agent(model=model1)
    agent1.execute_tool_call("safe_tool", {"query": "test"})
    checkpoint = _fresh_checkpoint(agent1._checkpoint)

    # Phase 2: replay from checkpoint
    model2 = TrackingModel([])
    agent2 = make_agent(model=model2, checkpoint=checkpoint)
    result = agent2.execute_tool_call("safe_tool", {"query": "test"})

    assert "result for test" in result
    assert agent2.audit_log[0]["replayed"] is True
    # Budget should still be deducted during replay
    assert agent2.capabilities["network"].current_usage == 1.0


def test_replay_serves_cached_llm():
    # Phase 1: record
    model1 = TrackingModel(["original response"])
    agent1 = make_agent(model=model1)
    agent1.model.generate([{"role": "user", "content": "hi"}])
    assert model1.call_count == 1
    checkpoint = _fresh_checkpoint(agent1._checkpoint)

    # Phase 2: replay
    model2 = TrackingModel([])
    agent2 = make_agent(model=model2, checkpoint=checkpoint)
    result = agent2.model.generate([{"role": "user", "content": "hi"}])

    assert result.content == "original response"
    assert model2.call_count == 0  # inner model NOT called


# -- Test: HITL suspend/resume --


def test_hitl_suspends_and_saves():
    agent = make_agent()
    with pytest.raises(HITLSuspendError) as exc_info:
        agent.execute_tool_call("destructive_tool", {"target": "x"})

    cp = exc_info.value.checkpoint
    assert cp.status == "SUSPENDED_FOR_HITL"
    assert cp.pending_hitl == {
        "tool_name": "destructive_tool",
        "arguments": {"target": "x"},
    }
    assert agent.capabilities["network"].current_usage == 2.0


def test_hitl_resume_replays_then_continues():
    # Phase 1: record LLM + safe_tool, then hit HITL on destructive_tool
    model1 = TrackingModel(["plan response"])
    agent1 = make_agent(model=model1)

    agent1.model.generate([{"role": "user", "content": "do something"}])
    agent1.execute_tool_call("safe_tool", {"query": "research"})

    with pytest.raises(HITLSuspendError):
        agent1.execute_tool_call("destructive_tool", {"target": "send it"})

    checkpoint = _fresh_checkpoint(agent1._checkpoint)
    approved_request = checkpoint.pending_hitl

    # Phase 2: approve and resume
    checkpoint.pending_hitl = None
    checkpoint.status = "RUNNING"

    model2 = TrackingModel([])
    agent2 = make_agent(
        model=model2,
        checkpoint=checkpoint,
        hitl_approved_request=approved_request,
    )

    # Replay LLM call (cached)
    result_llm = agent2.model.generate([{"role": "user", "content": "do something"}])
    assert result_llm.content == "plan response"
    assert model2.call_count == 0

    # Replay safe_tool (cached)
    result_tool = agent2.execute_tool_call("safe_tool", {"query": "research"})
    assert "result for research" in result_tool
    assert agent2.audit_log[0]["replayed"] is True

    # Live: destructive_tool should execute (HITL approved)
    result_destructive = agent2.execute_tool_call(
        "destructive_tool", {"target": "send it"}
    )
    assert "destroyed" in result_destructive
    assert agent2.audit_log[-1]["replayed"] is False
