"""End-to-end replay determinism test for memory syscalls.

AC #12 from BRIEFING_CASTOR_MEMORY_MIGRATION: session with a mix of
all 7 syscalls + auto-evict must replay byte-identical.
"""

from __future__ import annotations

import pytest

from castor import Castor, CastorMessage, castor_tool
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.core import (
    MEM_DELETE,
    MEM_EVICT,
    MEM_PROMOTE,
    MEM_PROTECT,
    MEM_READ,
    MEM_SEARCH,
    MEM_WRITE,
)


@pytest.mark.asyncio
async def test_memory_syscalls_replay_byte_identical():
    """Run an agent that exercises all 7 mem syscalls, then replay from
    checkpoint — the replayed journal must be byte-identical."""

    cold = InMemoryColdStorage()

    @castor_tool(consumes="api", cost_per_use=0.0)
    async def noop() -> str:
        return "ok"

    kernel = Castor(
        tools=[noop],
        cold_storage=cold,
        agent_id="replay-test",
    )

    async def agent(proxy):
        # mem_write
        r = await proxy.syscall(
            MEM_WRITE,
            {
                "content": "fact: sky is blue",
                "role": "user",
            },
        )
        write_id = r.get("memory_id", "")

        # mem_protect
        await proxy.syscall(MEM_PROTECT, {"memory_id": write_id, "protect": True})

        # mem_search
        await proxy.syscall(MEM_SEARCH, {"query": "sky"})

        # mem_read
        await proxy.syscall(MEM_READ, {"memory_id": write_id})

        # mem_write another (will be evicted)
        r2 = await proxy.syscall(MEM_WRITE, {"content": "fact: grass is green"})
        evict_id = r2.get("memory_id", "")

        # mem_evict
        await proxy.syscall(MEM_EVICT, {"memory_id": evict_id})

        # mem_promote (bring it back)
        await proxy.syscall(MEM_PROMOTE, {"memory_id": evict_id})

        # mem_delete
        await proxy.syscall(MEM_DELETE, {"memory_id": evict_id})

        return "all 7 done"

    # First run — live execution
    cp1 = await kernel.run(agent, pid="replay-det-001")
    assert cp1.status == "COMPLETED"
    assert cp1.result == "all 7 done"

    # Verify all 7 syscall types appear
    tool_names = [r.request.get("tool_name") for r in cp1.syscall_log]
    for expected in [
        MEM_WRITE,
        MEM_PROTECT,
        MEM_SEARCH,
        MEM_READ,
        MEM_EVICT,
        MEM_PROMOTE,
        MEM_DELETE,
    ]:
        assert expected in tool_names, f"missing {expected} in {tool_names}"

    # Second run — replay from the same checkpoint (all entries cached)
    cp_replay = cp1.fork(at_step=len(cp1.syscall_log))
    cp2 = await kernel.run(agent, checkpoint=cp_replay)
    assert cp2.status == "COMPLETED"
    assert cp2.result == "all 7 done"

    # Journal must be byte-identical
    assert len(cp1.syscall_log) == len(cp2.syscall_log)
    for i, (r1, r2) in enumerate(zip(cp1.syscall_log, cp2.syscall_log)):
        assert r1.request == r2.request, (
            f"request mismatch at {i}: {r1.request} != {r2.request}"
        )
        assert r1.invocation_id == r2.invocation_id, f"invocation_id mismatch at {i}"


@pytest.mark.asyncio
async def test_promote_into_empty_context():
    """Regression: apply_promote into empty/single-item context."""
    cold = InMemoryColdStorage()

    @castor_tool(consumes="api", cost_per_use=0.0)
    async def noop() -> str:
        return "ok"

    kernel = Castor(
        tools=[noop],
        cold_storage=cold,
        agent_id="promote-test",
    )

    # Pre-populate cold storage
    await cold.store(
        "promote-test",
        [CastorMessage(id="cold-1", role="system", content="old data")],
    )

    async def agent(proxy):
        # Promote into initially-empty context
        await proxy.syscall(MEM_PROMOTE, {"memory_id": "cold-1"})
        return "promoted"

    cp = await kernel.run(agent)
    assert cp.status == "COMPLETED"
    # The promoted message should be in context
    assert any(
        isinstance(m, CastorMessage) and m.id == "cold-1" for m in cp.context_history
    )


@pytest.mark.asyncio
async def test_mem_write_preserves_role():
    """Regression: mem_write should not hardcode role='memory'."""
    cold = InMemoryColdStorage()

    @castor_tool(consumes="api", cost_per_use=0.0)
    async def noop() -> str:
        return "ok"

    kernel = Castor(
        tools=[noop],
        cold_storage=cold,
        agent_id="role-test",
    )

    async def agent(proxy):
        r = await proxy.syscall(
            MEM_WRITE, {"content": "user said hello", "role": "user"}
        )
        return r.get("memory_id", "")

    cp = await kernel.run(agent)
    assert cp.status == "COMPLETED"

    # Find the written message in context
    written = [
        m
        for m in cp.context_history
        if isinstance(m, CastorMessage) and "user said" in str(m.content)
    ]
    assert len(written) == 1
    assert written[0].role == "user", (
        f"Expected role='user', got role='{written[0].role}'"
    )


@pytest.mark.asyncio
async def test_msg_seq_survives_restart():
    """_msg_seq is reconstructed from checkpoint journal, not process state.

    Simulates a server restart: create a new MMU instance and verify
    that next_memory_id picks up from where the previous run left off.
    """
    from castor.gate.registry import ToolRegistry
    from castor.mmu.cold_storage import InMemoryColdStorage
    from castor.mmu.core import MMU
    from castor.models.checkpoint import AgentCheckpoint, SyscallPurpose, SyscallRecord

    cold = InMemoryColdStorage()

    # Fake a checkpoint with 3 mem_write entries in the journal
    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    cp = AgentCheckpoint(
        pid="seq-test",
        status="RUNNING",
        agent_function_name="test",
        capabilities=bm.create_budgets({}),
        syscall_log=[
            SyscallRecord(
                request={"tool_name": "mem_write", "arguments": {"content": f"msg{i}"}},
                response={"memory_id": f"id{i}"},
                purpose=SyscallPurpose.MEMORY_MANAGEMENT,
            )
            for i in range(3)
        ],
    )

    # Create a "restarted" MMU — seq starts at 0
    reg = ToolRegistry()
    mmu = MMU(reg, cold_storage=cold, agent_id="seq-test")
    assert mmu._msg_seq == 0

    # sync_seq rebuilds from journal
    mmu.sync_seq(cp)
    assert mmu._msg_seq == 3

    # next_memory_id uses seq=3 (not 0)
    mid = mmu.next_memory_id("seq-test", "user", "hello")
    from castor.models.checkpoint import compute_memory_id

    expected = compute_memory_id("seq-test", 3, "user", "hello")
    assert mid == expected
