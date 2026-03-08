"""Integration tests for MMU: eviction + page-in + replay determinism.

These tests exercise the full kernel workflow with MMU enabled:
agent execution, automatic eviction before LLM calls, search_memory
page-in, HITL suspension, and replay determinism.
"""

from __future__ import annotations

import pytest

from castor.capability.manager import CapabilityManager
from castor.dam.decorator import castor_tool
from castor.dam.registry import ToolRegistry
from castor.dam.validator import CastorDam
from castor.llm.wrapper import LLMSyscall
from castor.mmu.core import MMU, PAGE_OUT_TOOL, SEARCH_MEMORY_TOOL
from castor.mmu.drivers.mock_driver import InMemoryDriver
from castor.models.checkpoint import AgentCheckpoint, CastorMessage
from castor.stream.hitl import HITLHandler
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner

# ── Fixtures ──


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def driver():
    return InMemoryDriver()


@pytest.fixture
def lodge(registry, driver):
    return MMU(
        registry,
        driver,
        watermark=50,  # low watermark to trigger eviction easily
        consumes="system",
        cost_per_use=0.0,
    )


@pytest.fixture
def llm_call_log():
    return []


@pytest.fixture
def llm(registry, llm_call_log):
    async def fake_llm(model: str, prompt: str) -> str:
        llm_call_log.append(prompt)
        return f"LLM response for: {prompt[:30]}"

    return LLMSyscall(registry, call_fn=fake_llm, consumes="network", cost_per_use=1.0)


@pytest.fixture
def dam(registry):
    @castor_tool(consumes="network", cost_per_use=0.5, registry=registry)
    def summarize(text: str) -> str:
        return f"Summary of: {text[:20]}"

    return CastorDam(registry)


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


@pytest.fixture
def hitl():
    return HITLHandler()


def _make_checkpoint(cap_mgr, messages=None):
    caps = cap_mgr.create_capabilities({"network": 100.0, "system": 100.0})
    cp = AgentCheckpoint(
        pid="lodge-integ-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )
    if messages:
        cp.context_history = list(messages)
    return cp


# ── Test 1: Eviction triggers before LLM call, page-in retrieves data ──


class TestEvictionAndPageIn:
    async def test_eviction_and_search_memory(self, dam, cap_mgr, lodge, llm, driver):
        """Large context triggers eviction before LLM, search retrieves it."""
        messages = [
            CastorMessage(
                role="system", content="You are helpful.", pinned=True, token_count=10
            ),
            CastorMessage(
                role="user", content="Tell me about battery technology", token_count=30
            ),
            CastorMessage(
                role="assistant",
                content="Battery tech is advancing rapidly",
                token_count=25,
            ),
        ]
        checkpoint = _make_checkpoint(cap_mgr, messages)

        async def agent_fn(proxy: SyscallProxy) -> str:
            # This LLM call should trigger eviction (total=65 > watermark=50)
            await llm.infer(proxy, model="gpt-4", prompt="Analyze findings")
            # Now search for the evicted content
            result = await proxy.syscall(
                SEARCH_MEMORY_TOOL,
                {"query": "battery", "pid": checkpoint.pid},
            )
            return result

        runner = AgentRunner(dam, cap_mgr, lodge=lodge)
        result = await runner.run(agent_fn, checkpoint)

        assert result.status == "COMPLETED"

        # Verify syscall sequence: page_out + llm_inference + search_memory
        tool_names = [r.request["tool_name"] for r in result.syscall_log]
        assert PAGE_OUT_TOOL in tool_names
        assert "llm_inference" in tool_names
        assert SEARCH_MEMORY_TOOL in tool_names

        # Pinned system message survived
        pinned = [
            m
            for m in result.context_history
            if isinstance(m, CastorMessage) and m.pinned
        ]
        assert len(pinned) == 1
        assert pinned[0].content == "You are helpful."

        # Search found the evicted content
        search_record = next(
            r
            for r in result.syscall_log
            if r.request["tool_name"] == SEARCH_MEMORY_TOOL
        )
        assert "battery" in search_record.response.lower()


# ── Test 2: Pinned messages survive even extreme eviction ──


class TestPinnedSurvival:
    async def test_pinned_messages_never_evicted(self, dam, cap_mgr, lodge, llm):
        """Even with massive context, pinned messages are never evicted."""
        messages = [
            CastorMessage(
                role="system", content="System prompt", pinned=True, token_count=10
            ),
            CastorMessage(
                role="user", content="HITL approved action", pinned=True, token_count=10
            ),
            CastorMessage(
                role="user", content="expendable message " * 5, token_count=40
            ),
        ]
        checkpoint = _make_checkpoint(cap_mgr, messages)

        async def agent_fn(proxy: SyscallProxy) -> str:
            await llm.infer(proxy, model="gpt-4", prompt="test")
            return "done"

        runner = AgentRunner(dam, cap_mgr, lodge=lodge)
        result = await runner.run(agent_fn, checkpoint)

        assert result.status == "COMPLETED"
        # Both pinned messages survive
        pinned = [
            m
            for m in result.context_history
            if isinstance(m, CastorMessage) and m.pinned
        ]
        assert len(pinned) == 2


# ── Test 3: Replay determinism — driver not called on replay ──


class TestEvictionReplayDeterminism:
    async def test_driver_not_called_on_replay(
        self, registry, dam, cap_mgr, lodge, llm, llm_call_log, driver, hitl
    ):
        """After HITL approve + replay, driver.ingest is NOT called again."""

        # Register a destructive tool to trigger HITL
        @castor_tool(
            consumes="network",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        def dangerous_action(action: str) -> str:
            return f"executed: {action}"

        # Rebuild dam with new tool
        dam = CastorDam(registry)

        messages = [
            CastorMessage(role="system", content="sys", pinned=True, token_count=5),
            CastorMessage(role="user", content="old context data", token_count=60),
        ]
        checkpoint = _make_checkpoint(cap_mgr, messages)

        ingest_count = 0
        original_ingest = driver.ingest

        async def tracking_ingest(msgs, pid):
            nonlocal ingest_count
            ingest_count += 1
            return await original_ingest(msgs, pid)

        driver.ingest = tracking_ingest

        async def agent_fn(proxy: SyscallProxy) -> str:
            # LLM call triggers eviction
            await llm.infer(proxy, model="gpt-4", prompt="plan")
            # Destructive action triggers HITL
            await proxy.syscall("dangerous_action", {"action": "deploy"})
            return "done"

        # Run 1: eviction + LLM → suspends at dangerous_action
        runner1 = AgentRunner(dam, cap_mgr, lodge=lodge)
        await runner1.run(agent_fn, checkpoint)

        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert ingest_count == 1
        assert len(llm_call_log) == 1

        # Approve HITL
        await hitl.approve(checkpoint, dam, cap_mgr)

        # Run 2: full replay — driver.ingest must NOT be called again
        runner2 = AgentRunner(dam, cap_mgr, lodge=lodge)
        result = await runner2.run(agent_fn, checkpoint)

        assert result.status == "COMPLETED"
        # Driver was NOT called during replay
        assert ingest_count == 1
        # LLM was NOT called during replay
        assert len(llm_call_log) == 1
