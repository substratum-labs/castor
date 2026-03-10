"""Tests for the OpenClaw agent example.

Exercises the full Castor kernel lifecycle: tool registration, LLM replay
safety, HITL suspension/approval/rejection, and budget tracking.
"""

from __future__ import annotations

import pytest

from castor import Castor, SyscallGate
from castor.gate.registry import ToolRegistry
from castor.llm.wrapper import LLMSyscall
from castor.scheduler.proxy import SyscallProxy
from examples.openclaw_agent.agent import openclaw_agent
from examples.openclaw_agent.tools import register_tools

# ── Fixtures ──


@pytest.fixture
def knowledge_base(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    return kb


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def llm_call_log():
    """Tracks how many times the fake LLM is called."""
    return []


@pytest.fixture
def llm(registry, llm_call_log):
    responses = iter(
        [
            "Plan: search, read notes, write summary, compose message, send.",
            "Battery tech summary: solid-state up 40%, costs down 25%.",
        ]
    )

    async def fake_llm(model: str, prompt: str) -> str:
        llm_call_log.append({"model": model, "prompt": prompt})
        return next(responses, f"[fallback for: {prompt[:40]}]")

    return LLMSyscall(registry, call_fn=fake_llm, consumes="network", cost_per_use=1.0)


@pytest.fixture
def gate(registry, knowledge_base):
    register_tools(registry, knowledge_base)
    return SyscallGate(registry)


@pytest.fixture
def kernel(gate):
    return Castor(gate=gate)


def _make_agent_fn(llm):
    """Wrap openclaw_agent with the LLM instance."""

    async def agent_fn(proxy: SyscallProxy) -> str:
        return await openclaw_agent(proxy, llm)

    return agent_fn


# ── Test 1: Full flow with HITL approval ──


class TestFullFlowApprove:
    async def test_approve_completes_all_steps(self, kernel, llm, llm_call_log):
        """Agent researches, suspends at send_message, human approves, completes."""
        agent_fn = _make_agent_fn(llm)

        # Run 1: executes until send_message → suspends
        cp = await kernel.run(
            agent_fn, budgets={"network": 50.0, "disk": 20.0}, pid="test-openclaw-001"
        )

        assert cp.status == "SUSPENDED_FOR_HITL"
        assert cp.pending_hitl["tool_name"] == "send_message"
        assert cp.pending_hitl["arguments"]["platform"] == "slack"
        # 5 syscalls: llm, web_search, read_note, write_note, llm
        assert len(cp.syscall_log) == 5
        assert len(llm_call_log) == 2

        # Human approves
        await kernel.approve(cp)
        assert cp.status == "RUNNING"
        assert len(cp.syscall_log) == 6  # + send_message

        # Run 2: replay from top, all cached, continues past HITL
        result = await kernel.run(agent_fn, checkpoint=cp)

        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 6
        # LLM was NOT called again during replay
        assert len(llm_call_log) == 2

        # Verify syscall sequence
        tool_names = [r.request["tool_name"] for r in result.syscall_log]
        assert tool_names == [
            "llm_inference",
            "web_search",
            "read_note",
            "write_note",
            "llm_inference",
            "send_message",
        ]
        assert result.syscall_log[5].was_hitl is True


# ── Test 2: HITL rejection triggers fallback ──


class TestRejectFallback:
    async def test_reject_saves_draft_instead(self, kernel, llm):
        """When human rejects send_message, agent writes a draft note instead."""
        agent_fn = _make_agent_fn(llm)

        # Run 1: suspends at send_message
        cp = await kernel.run(
            agent_fn, budgets={"network": 50.0, "disk": 20.0}, pid="test-openclaw-001"
        )
        assert cp.status == "SUSPENDED_FOR_HITL"

        # Human rejects
        kernel.reject(cp, "Don't send to Slack, save as draft.")

        # Run 2: replay → agent sees rejection → writes draft note
        result = await kernel.run(agent_fn, checkpoint=cp)

        assert result.status == "COMPLETED"
        # 5 original + send_message(rejected) + write_note(draft) = 7
        assert len(result.syscall_log) == 7

        # Verify the fallback: last syscall is write_note for the draft
        rejected_record = result.syscall_log[5]
        assert rejected_record.request["tool_name"] == "send_message"
        assert rejected_record.response["status"] == "HITL_REJECTED"

        draft_record = result.syscall_log[6]
        assert draft_record.request["tool_name"] == "write_note"
        assert "draft" in draft_record.request["arguments"]["filename"]


# ── Test 3: Budget tracking ──


class TestBudgetTracking:
    async def test_budget_deducted_correctly(self, kernel, llm):
        """Capability budgets reflect exact tool costs after full run."""
        agent_fn = _make_agent_fn(llm)

        # Run to suspension
        cp = await kernel.run(
            agent_fn, budgets={"network": 50.0, "disk": 20.0}, pid="test-openclaw-001"
        )

        # Approve and resume
        await kernel.approve(cp)
        result = await kernel.run(agent_fn, checkpoint=cp)

        assert result.status == "COMPLETED"

        # Network: llm(1) + search(1) + llm(1) + send(2) = 5.0
        assert result.capabilities["network"].current_usage == 5.0

        # Expected disk costs:
        #   read_note(0.5) + write_note(1.0) = 1.5
        assert result.capabilities["disk"].current_usage == 1.5
