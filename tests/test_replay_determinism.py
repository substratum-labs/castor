"""Tests that LLM inference routed through the proxy is replay-deterministic.

The core scenario: an agent calls an LLM via LLMSyscall, then hits a
destructive tool (HITL slow path), suspends, is approved, and resumes.
On resume the LLM provider must NOT be called again — the cached response
from the syscall_log is served instead.
"""

from __future__ import annotations

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.llm.wrapper import LLMSyscall
from castor.models.checkpoint import AgentCheckpoint
from castor.stream.hitl import HITLHandler
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


@pytest.fixture
def hitl():
    return HITLHandler()


def _make_checkpoint(cap_mgr):
    caps = cap_mgr.create_capabilities({"api_usd": 10.0, "disk": 10.0})
    return AgentCheckpoint(
        pid="llm-agent-001",
        status="RUNNING",
        agent_function_name="llm_agent",
        capabilities=caps,
    )


class TestLLMReplayDeterminism:
    """LLM responses must be served from cache on replay, never re-fetched."""

    async def test_llm_not_called_on_replay_after_hitl(
        self, registry, gate, cap_mgr, hitl
    ):
        # ── Arrange: mock LLM client with call tracking ──
        call_log: list[dict] = []

        async def fake_llm(model: str, prompt: str) -> str:
            call_log.append({"model": model, "prompt": prompt})
            return "Generated plan: delete /tmp/old"

        llm = LLMSyscall(
            registry,
            call_fn=fake_llm,
            consumes="api_usd",
            cost_per_use=1.0,
        )

        @castor_tool(
            consumes="disk",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        def delete_files(paths: list[str]) -> int:
            return len(paths)

        @castor_tool(consumes="api_usd", cost_per_use=0.1, registry=registry)
        def notify(message: str) -> str:
            return f"notified: {message}"

        async def llm_agent(proxy: SyscallProxy) -> str:
            # Step 1: LLM inference (non-deterministic — must be cached)
            await llm.infer(proxy, model="gpt-4", prompt="What to clean?")
            # Step 2: destructive action based on LLM output (triggers HITL)
            result = await proxy.syscall("delete_files", {"paths": ["/tmp/old"]})
            # Step 3: follow-up after HITL approval
            await proxy.syscall("notify", {"message": f"done: {result}"})
            return "complete"

        # ── Act: first run — suspends at delete_files ──
        checkpoint = _make_checkpoint(cap_mgr)
        runner1 = AgentRunner(gate, cap_mgr)
        await runner1.run(llm_agent, checkpoint)

        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert checkpoint.pending_hitl["tool_name"] == "delete_files"
        assert len(checkpoint.syscall_log) == 1  # only llm_inference completed
        assert len(call_log) == 1

        # ── Human approves ──
        await hitl.approve(checkpoint, gate, cap_mgr)
        assert checkpoint.status == "RUNNING"
        assert len(checkpoint.syscall_log) == 2  # llm_inference + delete_files

        # ── Resume via replay ──
        runner2 = AgentRunner(gate, cap_mgr)
        result = await runner2.run(llm_agent, checkpoint)

        # ── Assert: LLM was NOT called again ──
        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 3
        assert len(call_log) == 1  # still 1 — served from cache

        # Verify the cached LLM response was used
        llm_record = result.syscall_log[0]
        assert llm_record.request["tool_name"] == "llm_inference"
        assert llm_record.response == "Generated plan: delete /tmp/old"

    async def test_llm_response_survives_full_replay(self, registry, gate, cap_mgr):
        """Run agent to completion, then replay entirely from cache."""
        call_count = 0

        async def counting_llm(model: str, prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"response #{call_count}"

        llm = LLMSyscall(
            registry,
            call_fn=counting_llm,
            consumes="api_usd",
            cost_per_use=0.5,
        )

        async def simple_agent(proxy: SyscallProxy) -> str:
            r1 = await llm.infer(proxy, model="gpt-4", prompt="first")
            r2 = await llm.infer(proxy, model="gpt-4", prompt="second")
            return f"{r1} | {r2}"

        # First run — two live LLM calls
        checkpoint = _make_checkpoint(cap_mgr)
        runner1 = AgentRunner(gate, cap_mgr)
        await runner1.run(simple_agent, checkpoint)

        assert checkpoint.status == "COMPLETED"
        assert call_count == 2
        assert checkpoint.syscall_log[0].response == "response #1"
        assert checkpoint.syscall_log[1].response == "response #2"

        # Full replay — zero live LLM calls (all served from log)
        runner2 = AgentRunner(gate, cap_mgr)
        replayed = await runner2.run(simple_agent, checkpoint)

        assert replayed.status == "COMPLETED"
        assert call_count == 2  # unchanged — no new calls
        assert replayed.syscall_log[0].response == "response #1"
        assert replayed.syscall_log[1].response == "response #2"

    async def test_llm_syscall_with_custom_tool_name(self, registry, gate, cap_mgr):
        """LLMSyscall can be registered under a custom tool name."""
        called = False

        async def custom_llm(model: str, prompt: str) -> str:
            nonlocal called
            called = True
            return "custom response"

        llm = LLMSyscall(
            registry,
            call_fn=custom_llm,
            consumes="api_usd",
            cost_per_use=0.1,
            tool_name="anthropic_claude",
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="claude-3", prompt="hello")

        checkpoint = _make_checkpoint(cap_mgr)
        runner = AgentRunner(gate, cap_mgr)
        await runner.run(agent, checkpoint)

        assert checkpoint.status == "COMPLETED"
        assert called is True
        assert checkpoint.syscall_log[0].request["tool_name"] == "anthropic_claude"
        assert checkpoint.syscall_log[0].response == "custom response"
