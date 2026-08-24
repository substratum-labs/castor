"""Causal-memory contracts for dependency-safe memory syscalls."""

from __future__ import annotations

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.mmu.core import MMU
from castor.models.causal import CascadeMode, ExternalSource, MemoryRef
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.proxy import SyscallProxy


def make_proxy() -> tuple[SyscallProxy, MMU]:
    budget = BudgetManager()
    registry = ToolRegistry()
    mmu = MMU(registry, agent_id="causal")
    checkpoint = AgentCheckpoint(
        pid="causal-test",
        status="RUNNING",
        agent_function_name="test",
        capabilities=budget.create_budgets({"api_usd": 1.0}),
    )
    return SyscallProxy(checkpoint, SyscallGate(registry), budget, lodge=mmu), mmu


@pytest.mark.asyncio
async def test_write_records_explicit_dependency_and_metadata():
    proxy, mmu = make_proxy()
    first = await proxy.syscall("mem_write", {"content": "source"})
    second = await proxy.syscall(
        "mem_write",
        {
            "content": "derived",
            "depends_on": [MemoryRef(memory_id=first["memory_id"]).model_dump()],
            "source_trust": 0.6,
            "reason": "derived from source",
        },
    )

    entry = mmu.find_by_id(proxy.checkpoint, second["memory_id"])
    assert entry is not None
    assert entry.depends_on == [MemoryRef(memory_id=first["memory_id"])]
    assert entry.source_trust == pytest.approx(0.6)
    assert entry.reason == "derived from source"


@pytest.mark.asyncio
async def test_forbid_evict_refuses_memory_with_deriver_without_mutation():
    proxy, mmu = make_proxy()
    root = await proxy.syscall("mem_write", {"content": "root"})
    derived = await proxy.syscall(
        "mem_write",
        {
            "content": "derived",
            "depends_on": [MemoryRef(memory_id=root["memory_id"]).model_dump()],
        },
    )

    result = await proxy.syscall(
        "mem_evict", {"memory_id": root["memory_id"], "cascade": CascadeMode.FORBID}
    )

    assert result == {
        "evicted": [],
        "orphaned": [derived["memory_id"]],
        "refused": True,
    }
    assert mmu.find_by_id(proxy.checkpoint, root["memory_id"]) is not None


@pytest.mark.asyncio
async def test_cascade_evicts_root_and_transitive_derivers():
    proxy, mmu = make_proxy()
    root = await proxy.syscall("mem_write", {"content": "root"})
    middle = await proxy.syscall(
        "mem_write",
        {
            "content": "middle",
            "depends_on": [{"kind": "memory", "memory_id": root["memory_id"]}],
        },
    )
    leaf = await proxy.syscall(
        "mem_write",
        {
            "content": "leaf",
            "depends_on": [{"kind": "memory", "memory_id": middle["memory_id"]}],
        },
    )

    result = await proxy.syscall(
        "mem_evict", {"memory_id": root["memory_id"], "cascade": "cascade"}
    )

    assert result["evicted"] == [
        leaf["memory_id"],
        middle["memory_id"],
        root["memory_id"],
    ]
    assert all(
        mmu.find_by_id(proxy.checkpoint, memory_id) is None
        for memory_id in result["evicted"]
    )


@pytest.mark.asyncio
async def test_provenance_walk_includes_external_source_and_honors_depth():
    proxy, _ = make_proxy()
    root = await proxy.syscall("mem_write", {"content": "root"})
    child = await proxy.syscall(
        "mem_write",
        {
            "content": "child",
            "depends_on": [
                {"kind": "memory", "memory_id": root["memory_id"]},
                ExternalSource(
                    uri="web://example.test/a",
                    fetched_at="2026-08-24T00:00:00Z",
                    digest="a" * 64,
                ).model_dump(mode="json"),
            ],
        },
    )

    graph = await proxy.syscall(
        "mem_provenance",
        {"memory_id": child["memory_id"], "direction": "sources", "max_depth": 1},
    )

    assert set(graph["nodes"]) == {
        child["memory_id"],
        root["memory_id"],
        "web://example.test/a",
    }
    assert graph["truncated_at_max_depth"] is False


@pytest.mark.asyncio
async def test_explain_uses_graph_data_without_llm():
    proxy, _ = make_proxy()
    root = await proxy.syscall(
        "mem_write", {"content": "root fact", "reason": "observed"}
    )
    child = await proxy.syscall(
        "mem_write",
        {
            "content": "derived conclusion",
            "depends_on": [{"kind": "memory", "memory_id": root["memory_id"]}],
        },
    )

    explanation = await proxy.syscall(
        "mem_explain", {"memory_id": child["memory_id"], "style": "chain"}
    )

    assert "derived conclusion" in explanation
    assert "root fact" in explanation
