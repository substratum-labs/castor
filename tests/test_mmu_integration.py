"""Integration tests for MMU: AISA §2.2 memory syscalls through kernel.

All 7 syscalls dispatched through proxy.syscall(), verified in journal
with purpose=MEMORY_MANAGEMENT.
"""

from __future__ import annotations

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.core import (
    MEM_DELETE,
    MEM_EVICT,
    MEM_PROMOTE,
    MEM_PROTECT,
    MEM_READ,
    MEM_SEARCH,
    MEM_WRITE,
    MMU,
)
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
    return MMU(registry, cold_storage=cold_storage, hard_watermark=50, agent_id="integ")


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def budget_mgr():
    return BudgetManager()


def _make_cp(budget_mgr, msgs=None):
    budgets = budget_mgr.create_budgets({"network": 100.0, "system": 100.0})
    cp = AgentCheckpoint(
        pid="integ-001",
        status="RUNNING",
        agent_function_name="test",
        capabilities=budgets,
    )
    if msgs:
        cp.context_history = list(msgs)
    return cp


class TestMemWrite:
    @pytest.mark.asyncio
    async def test_write_through_syscall(self, gate, budget_mgr, lodge, cold_storage):
        async def agent(proxy: SyscallProxy):
            result = await proxy.syscall(
                MEM_WRITE, {"content": "important", "metadata": {"tag": "test"}}
            )
            return result

        cp = _make_cp(budget_mgr)
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)
        assert rcp.status == "COMPLETED"

        records = [
            r for r in rcp.syscall_log if r.request.get("tool_name") == MEM_WRITE
        ]
        assert len(records) == 1
        assert records[0].purpose == SyscallPurpose.MEMORY_MANAGEMENT

        # Message should be in context_history with an ID
        assert any(
            isinstance(m, CastorMessage) and m.content == "important"
            for m in rcp.context_history
        )


class TestMemEvict:
    @pytest.mark.asyncio
    async def test_evict_single_by_id(self, gate, budget_mgr, lodge, cold_storage):
        async def agent(proxy: SyscallProxy):
            await proxy.syscall(MEM_EVICT, {"memory_id": "msg_a"})
            return "done"

        cp = _make_cp(
            budget_mgr,
            [
                CastorMessage(id="msg_a", role="user", content="evict me"),
                CastorMessage(id="msg_b", role="user", content="keep me"),
            ],
        )
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)
        assert len(rcp.context_history) == 1
        assert rcp.context_history[0].id == "msg_b"
        assert cold_storage.count("integ") >= 1


class TestMemProtect:
    @pytest.mark.asyncio
    async def test_protect_by_id(self, gate, budget_mgr, lodge):
        async def agent(proxy: SyscallProxy):
            await proxy.syscall(MEM_PROTECT, {"memory_id": "x", "protect": True})
            return "done"

        cp = _make_cp(budget_mgr, [CastorMessage(id="x", role="u", content="test")])
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)
        assert rcp.context_history[0].pinned is True


class TestMemSearch:
    @pytest.mark.asyncio
    async def test_search_cold(self, gate, budget_mgr, lodge, cold_storage):
        await cold_storage.store_explicit("integ", "tables info", memory_id="m1")

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall(MEM_SEARCH, {"query": "tables"})

        cp = _make_cp(budget_mgr)
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)
        assert rcp.status == "COMPLETED"
        result = rcp.result
        assert isinstance(result, dict)
        assert len(result.get("results", [])) >= 1


class TestMemRead:
    @pytest.mark.asyncio
    async def test_read_from_cold(self, gate, budget_mgr, lodge, cold_storage):
        await cold_storage.store_explicit("integ", "secret data", memory_id="m1")

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall(MEM_READ, {"memory_id": "m1"})

        cp = _make_cp(budget_mgr)
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)
        result = rcp.result
        assert result.get("location") == "COLD_STORAGE"
        assert "secret" in result.get("content", "")


class TestMemDelete:
    @pytest.mark.asyncio
    async def test_delete_from_cold(self, gate, budget_mgr, lodge, cold_storage):
        await cold_storage.store_explicit("integ", "temp", memory_id="tmp")

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall(MEM_DELETE, {"memory_id": "tmp"})

        cp = _make_cp(budget_mgr)
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)
        assert rcp.result.get("deleted") is True
        assert cold_storage.count("integ") == 0


class TestMemPromote:
    @pytest.mark.asyncio
    async def test_promote_from_cold(self, gate, budget_mgr, lodge, cold_storage):
        await cold_storage.store(
            "integ",
            [CastorMessage(id="cold_msg", role="user", content="old data")],
        )

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall(MEM_PROMOTE, {"memory_id": "cold_msg"})

        cp = _make_cp(budget_mgr, [CastorMessage(id="q", role="u", content="current")])
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)

        assert rcp.result.get("promoted") is True
        contents = [
            m.content for m in rcp.context_history if isinstance(m, CastorMessage)
        ]
        assert any("old data" in c for c in contents)


class TestCrossSessionRecall:
    @pytest.mark.asyncio
    async def test_not_filtered_by_session(self, gate, budget_mgr, cold_storage):
        reg = ToolRegistry()
        mmu = MMU(
            reg, cold_storage=cold_storage, hard_watermark=1000, agent_id="shared"
        )
        await cold_storage.store(
            "shared", [CastorMessage(id="s1", role="u", content="session A knowledge")]
        )

        async def agent_b(proxy: SyscallProxy):
            return await proxy.syscall(MEM_SEARCH, {"query": "session A"})

        cp = _make_cp(budget_mgr)
        runner = AgentRunner(SyscallGate(reg), budget_mgr, lodge=mmu)
        rcp = await runner.run(agent_b, cp)
        results = rcp.result.get("results", [])
        assert len(results) >= 1


class TestPurposeTagging:
    @pytest.mark.asyncio
    async def test_all_memory_syscalls_tagged(
        self, gate, budget_mgr, lodge, cold_storage
    ):
        await cold_storage.store_explicit("integ", "data", memory_id="d1")

        async def agent(proxy: SyscallProxy):
            await proxy.syscall(MEM_WRITE, {"content": "fact"})
            await proxy.syscall(MEM_SEARCH, {"query": "data"})
            await proxy.syscall(MEM_READ, {"memory_id": "d1"})
            return "done"

        cp = _make_cp(budget_mgr)
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)

        for r in rcp.syscall_log:
            tn = r.request.get("tool_name", "")
            if tn.startswith("mem_"):
                assert r.purpose == SyscallPurpose.MEMORY_MANAGEMENT, (
                    f"{tn} has purpose={r.purpose}"
                )


class TestPauseAutoEvict:
    @pytest.mark.asyncio
    async def test_manual_evict_works_when_paused(self, gate, budget_mgr, lodge):
        async def agent(proxy: SyscallProxy):
            await proxy.syscall(MEM_EVICT, {"memory_id": "a"})
            return "done"

        cp = _make_cp(
            budget_mgr,
            [
                CastorMessage(id="a", role="u", content="evict"),
                CastorMessage(id="b", role="u", content="keep"),
            ],
        )
        lodge.pause_auto_evict()
        runner = AgentRunner(gate, budget_mgr, lodge=lodge)
        rcp = await runner.run(agent, cp)
        assert len(rcp.context_history) == 1
        assert rcp.context_history[0].id == "b"
