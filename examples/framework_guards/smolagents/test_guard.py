"""Tests for CastorGuardedAgent -- budget enforcement and HITL gates."""

from __future__ import annotations

import sys

import pytest
from smolagents import tool
from smolagents.models import ChatMessage, Model

from castor.capability.manager import CapabilityExhaustedError

sys.path.insert(0, ".")
from examples.framework_guards.smolagents.guard import CastorGuardedAgent, ToolRejectedError

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


class FakeModel(Model):
    """Minimal model stub satisfying ToolCallingAgent.__init__.

    Subclasses smolagents.models.Model so the agent init
    doesn't choke on missing attributes.
    """

    def __init__(self):
        super().__init__(model_id="fake")

    def generate(self, messages, **kwargs):
        return ChatMessage(role="assistant", content="done")


def make_agent(budgets, hitl_policy=None):
    return CastorGuardedAgent(
        tools=[safe_tool, destructive_tool],
        model=FakeModel(),
        budgets=budgets,
        tool_policies=POLICIES,
        hitl_policy=hitl_policy,
    )


# -- Tests --


def test_budget_deduction():
    agent = make_agent(budgets={"network": 10.0})
    agent.execute_tool_call("safe_tool", {"query": "test"})
    assert agent.capabilities["network"].current_usage == 1.0
    assert len(agent.audit_log) == 1
    assert agent.audit_log[0]["tool"] == "safe_tool"


def test_budget_exhausted():
    agent = make_agent(budgets={"network": 0.5})
    with pytest.raises(CapabilityExhaustedError):
        agent.execute_tool_call("safe_tool", {"query": "test"})
    assert len(agent.audit_log) == 0


def test_hitl_reject():
    agent = make_agent(
        budgets={"network": 10.0},
        hitl_policy=lambda name, args: False,
    )
    with pytest.raises(ToolRejectedError):
        agent.execute_tool_call("destructive_tool", {"target": "x"})
    # Budget was deducted but tool didn't execute
    assert agent.capabilities["network"].current_usage == 2.0
    assert len(agent.audit_log) == 0


def test_hitl_approve():
    agent = make_agent(
        budgets={"network": 10.0},
        hitl_policy=lambda name, args: True,
    )
    result = agent.execute_tool_call("destructive_tool", {"target": "x"})
    assert "destroyed" in result
    assert agent.capabilities["network"].current_usage == 2.0
    assert len(agent.audit_log) == 1
