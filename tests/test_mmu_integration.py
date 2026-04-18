"""Integration tests for MMU: eviction through syscall + replay determinism.

These exercise the full kernel workflow with the new memory syscalls:
mem_evict, mem_recall, mem_pin, mem_store — all routed through
proxy.syscall() and verified in the journal.
"""

from __future__ import annotations

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.llm.wrapper import LLMSyscall
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.core import MEM_EVICT, MEM_PIN, MEM_RECALL, MEM_STORE, MMU
from castor.models.checkpoint import AgentCheckpoint, CastorMessage, SyscallPurpose
from castor.scheduler.proxy import SyscallProxy
from castor.scheduler.runner import AgentRunner


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def cold_storage():
    return InMemoryColdStorage()


@pytest.fixture
def lodge(registry, cold_storage):
    return MMU(
        registry,
        cold_storage=cold_storage,
        hard_watermark=50,
        agent_id="integ-agent",
    )


@pytest.fixture
def llm(registry):
    async def fake_llm(model: str, prompt: str) -> str:
        return f"LLM response for: {prompt[:30]}"

    return LLMSyscall(registry, call_fn=fake_llm, consumes="network", cost_per_use=1.0)


@pytest.fixture
def gate(registry):
    @castor_tool(consumes="network", cost_per_use=0.5, registry=registry)
    def summarize(text: str) -> str:
        return f"Summary of: {text[:20]}"

    return SyscallGate(registry)


@pytest.fixture
def budget_mgr():
    return BudgetManager()


def _make_checkpoint(budget_mgr, messages=None):
    caps = budget_mgr.create_budgets({"network": 100.0, "system": 100.0})
    cp = AgentCheckpoint(
        pid="lodge-integ-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )
    if messages:
        cp.context_history = list(messages)
    return cp


class TestMemStoreSyscall:
    @pytest.mark.asyncio
    async def test_mem_store_through_syscall(
        self, gate, budget_mgr, lodge, cold_storage
    ):
        async def agent(proxy: SyscallProxy):
            return await proxy.syscall(
                MEM_STORE,
                {"content": "important fact", "metadata": {"tag": "test"}},
            )

        cp = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        result_cp = await runner.run(agent, cp)
        assert result_cp.status == "COMPLETED"

        store_records = [
            r for r in result_cp.syscall_log if r.request.get("tool_name") == MEM_STORE
        ]
        assert len(store_records) == 1
        assert store_records[0].purpose == SyscallPurpose.MEMORY_MANAGEMENT

        results = await cold_storage.search(
            "integ-agent", "important", source_filter="explicit"
        )
        assert len(results) >= 1


class TestMemPinSyscall:
    @pytest.mark.asyncio
    async def test_mem_pin_through_syscall(self, gate, budget_mgr, lodge):
        async def agent(proxy: SyscallProxy):
            await proxy.syscall(MEM_PIN, {"index": 0})
            return "done"

        cp = _make_checkpoint(
            budget_mgr,
            [CastorMessage(role="user", content="pin me")],
        )
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        result_cp = await runner.run(agent, cp)
        assert result_cp.status == "COMPLETED"
        assert result_cp.context_history[0].pinned is True


class TestMemRecallSyscall:
    @pytest.mark.asyncio
    async def test_mem_recall_through_syscall(
        self, gate, budget_mgr, lodge, cold_storage
    ):
        await cold_storage.store_explicit("integ-agent", "recalled fact about tables")

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall(
                MEM_RECALL, {"query": "tables", "max_results": 3}
            )

        cp = _make_checkpoint(
            budget_mgr,
            [CastorMessage(role="user", content="what tables?")],
        )
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        result_cp = await runner.run(agent, cp)
        assert result_cp.status == "COMPLETED"

        contents = [
            m.content for m in result_cp.context_history if isinstance(m, CastorMessage)
        ]
        assert any("tables" in c for c in contents)


class TestCrossSessionRecall:
    @pytest.mark.asyncio
    async def test_recall_not_filtered_by_session(self, gate, budget_mgr, cold_storage):
        registry = ToolRegistry()
        mmu = MMU(
            registry,
            cold_storage=cold_storage,
            hard_watermark=1000,
            agent_id="shared-agent",
        )

        await cold_storage.store(
            "shared-agent",
            [CastorMessage(role="user", content="session A knowledge")],
        )

        async def agent_b(proxy: SyscallProxy):
            return await proxy.syscall(
                MEM_RECALL, {"query": "session A", "max_results": 5}
            )

        gate_b = SyscallGate(registry)
        cp_b = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate_b, budget_mgr, lodge=mmu)
        result_cp = await runner.run(agent_b, cp_b)

        response = result_cp.result
        assert isinstance(response, dict)
        messages = response.get("messages", [])
        assert len(messages) >= 1
        assert any("session A" in str(m) for m in messages)


class TestPauseAutoEvict:
    @pytest.mark.asyncio
    async def test_manual_evict_works_when_paused(self, gate, budget_mgr, lodge):
        async def agent(proxy: SyscallProxy):
            await proxy.syscall(MEM_EVICT, {"indices": [0], "summary": None})
            return "done"

        cp = _make_checkpoint(
            budget_mgr,
            [
                CastorMessage(role="user", content="to evict"),
                CastorMessage(role="user", content="to keep"),
            ],
        )

        lodge.pause_auto_evict()
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        result_cp = await runner.run(agent, cp)

        assert len(result_cp.context_history) == 1
        assert result_cp.context_history[0].content == "to keep"
