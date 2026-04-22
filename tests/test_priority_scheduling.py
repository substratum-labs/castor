"""Tests for §1: Priority honoring in spawn scheduler.

Verifies that children are dispatched in priority order (highest first,
FIFO for same priority).
"""

from __future__ import annotations

import pytest

from castor import (
    AgentRegistry,
    Castor,
)

# ── Unit tests: dispatch queue ordering ──


def test_dispatch_order_by_priority():
    """Spawn 3 children with priorities [1, 10, 5]. Dispatch should
    return them in order [10, 5, 1]."""
    from castor.budget.manager import BudgetManager
    from castor.gate.registry import ToolRegistry
    from castor.gate.validator import SyscallGate
    from castor.models.checkpoint import AgentCheckpoint
    from castor.scheduler.proxy import SyscallProxy

    budget_mgr = BudgetManager()
    budgets = budget_mgr.create_budgets({"api": 100.0})
    cp = AgentCheckpoint(
        pid="parent",
        status="RUNNING",
        agent_function_name="test",
        capabilities=budgets,
    )
    proxy = SyscallProxy(cp, SyscallGate(ToolRegistry()), budget_mgr)

    async def dummy(p):
        pass

    # Enqueue in creation order: priority 1, 10, 5
    for pid, pri in [("child-1", 1), ("child-10", 10), ("child-5", 5)]:
        child_cp = AgentCheckpoint(
            pid=pid,
            status="RUNNING",
            agent_function_name="child",
            capabilities={},
            priority=pri,
        )
        proxy.enqueue_spawn(pid, dummy, child_cp)

    # Dispatch: should come out 10, 5, 1
    order = []
    while proxy.spawn_queue_size > 0:
        pid, _, _ = proxy.dispatch_next()
        order.append(pid)

    assert order == ["child-10", "child-5", "child-1"]


def test_fifo_within_same_priority():
    """Two children with priority=5 dispatched in creation order."""
    from castor.budget.manager import BudgetManager
    from castor.gate.registry import ToolRegistry
    from castor.gate.validator import SyscallGate
    from castor.models.checkpoint import AgentCheckpoint
    from castor.scheduler.proxy import SyscallProxy

    budget_mgr = BudgetManager()
    budgets = budget_mgr.create_budgets({"api": 100.0})
    cp = AgentCheckpoint(
        pid="parent",
        status="RUNNING",
        agent_function_name="test",
        capabilities=budgets,
    )
    proxy = SyscallProxy(cp, SyscallGate(ToolRegistry()), budget_mgr)

    async def dummy(p):
        pass

    for pid in ["first", "second"]:
        child_cp = AgentCheckpoint(
            pid=pid,
            status="RUNNING",
            agent_function_name="child",
            capabilities={},
            priority=5,
        )
        proxy.enqueue_spawn(pid, dummy, child_cp)

    r1 = proxy.dispatch_next()
    r2 = proxy.dispatch_next()
    assert r1[0] == "first"
    assert r2[0] == "second"


def test_empty_queue_returns_none():
    from castor.budget.manager import BudgetManager
    from castor.gate.registry import ToolRegistry
    from castor.gate.validator import SyscallGate
    from castor.models.checkpoint import AgentCheckpoint
    from castor.scheduler.proxy import SyscallProxy

    budget_mgr = BudgetManager()
    budgets = budget_mgr.create_budgets({"api": 100.0})
    cp = AgentCheckpoint(
        pid="parent",
        status="RUNNING",
        agent_function_name="test",
        capabilities=budgets,
    )
    proxy = SyscallProxy(cp, SyscallGate(ToolRegistry()), budget_mgr)
    assert proxy.dispatch_next() is None


# ── Integration test: priority through kernel ──


@pytest.mark.asyncio
async def test_priority_recorded_on_child_checkpoint():
    """sync spawn with priority=8 records priority on child checkpoint."""

    async def worker(proxy) -> str:
        return "done"

    async def fake_llm(model="", messages=None, tools=None):
        return {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    reg = AgentRegistry()
    reg.register("worker", worker)
    kernel = Castor(tools=[], llm=fake_llm, agent_registry=reg)

    async def parent(proxy):
        result = await proxy.syscall(
            "spawn_agent",
            {
                "agent_name": "worker",
                "capabilities": {},
                "priority": 8,
            },
        )
        return result

    cp = await kernel.run(parent)
    assert cp.status == "COMPLETED"

    # The spawn record should contain the child checkpoint with priority=8
    spawn_records = [
        r for r in cp.syscall_log if r.request.get("tool_name") == "spawn_agent"
    ]
    assert len(spawn_records) == 1
    child_cp = spawn_records[0].child_checkpoint
    assert child_cp is not None
    assert child_cp.priority == 8
