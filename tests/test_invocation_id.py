"""Tests for deterministic invocation_id on SyscallRecord.

Verifies that:
1. compute_invocation_id is deterministic (same inputs → same hash)
2. Different inputs produce different ids
3. proxy.syscall() attaches invocation_id to journal entries
4. HITL approve/reject/modify attach invocation_id
5. Replay produces the same invocation_ids as the original run
"""

from __future__ import annotations

import pytest

from castor import Castor, compute_invocation_id

# ---------------------------------------------------------------------------
# Unit tests for compute_invocation_id
# ---------------------------------------------------------------------------


def test_deterministic():
    """Same inputs always produce the same hash."""
    a = compute_invocation_id("pid_1", 0, "bash", {"command": "echo hi"})
    b = compute_invocation_id("pid_1", 0, "bash", {"command": "echo hi"})
    assert a == b
    assert len(a) == 32  # truncated sha256


def test_different_pid():
    a = compute_invocation_id("pid_1", 0, "bash", {"command": "echo hi"})
    b = compute_invocation_id("pid_2", 0, "bash", {"command": "echo hi"})
    assert a != b


def test_different_index():
    a = compute_invocation_id("pid_1", 0, "bash", {"command": "echo hi"})
    b = compute_invocation_id("pid_1", 1, "bash", {"command": "echo hi"})
    assert a != b


def test_identity_is_derived_only_from_pid_and_journal_index():
    a = compute_invocation_id("pid_1", 0, "bash", {"command": "echo hi"})
    b = compute_invocation_id("pid_1", 0, "read", {"command": "echo hi"})
    assert a == b


def test_tool_arguments_do_not_affect_operation_identity():
    a = compute_invocation_id("pid_1", 0, "bash", {"command": "echo hi"})
    b = compute_invocation_id("pid_1", 0, "bash", {"command": "echo bye"})
    assert a == b


# ---------------------------------------------------------------------------
# Integration: proxy attaches invocation_id to journal entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_id_attached_to_syscall_log():
    """After a normal tool call, the SyscallRecord has an invocation_id."""

    async def echo(text: str = "") -> str:
        return f"echo: {text}"

    kernel = Castor(tools=[echo])

    async def agent(proxy):
        return await proxy.syscall("echo", {"text": "hello"})

    cp = await kernel.run(agent)
    assert cp.status == "COMPLETED"
    assert len(cp.syscall_log) == 1

    record = cp.syscall_log[0]
    assert record.invocation_id is not None
    assert len(record.invocation_id) == 32


@pytest.mark.asyncio
async def test_kernel_injects_operation_id_into_every_tool_execution():
    """Tools receive the kernel-issued ID, never one supplied by the agent."""
    received_operation_ids: list[str] = []

    async def effect(
        amount: int, operation_id: str = "agent-controlled-default"
    ) -> str:
        received_operation_ids.append(operation_id)
        return "executed"

    kernel = Castor(tools=[effect])

    async def agent(proxy):
        return await proxy.syscall(
            "effect",
            {"amount": 7, "operation_id": "forged-by-agent"},
        )

    cp = await kernel.run(agent)

    assert cp.status == "COMPLETED"
    assert received_operation_ids == [cp.syscall_log[0].invocation_id]
    assert received_operation_ids != ["forged-by-agent"]


@pytest.mark.asyncio
async def test_invocation_id_stable_across_replay():
    """Replay of the same execution produces identical invocation_ids."""

    call_count = 0

    async def counter(n: int = 0) -> int:
        nonlocal call_count
        call_count += 1
        return n * 2

    kernel = Castor(tools=[counter])

    async def agent(proxy):
        a = await proxy.syscall("counter", {"n": 5})
        b = await proxy.syscall("counter", {"n": 10})
        return [a, b]

    # First run — live execution
    cp1 = await kernel.run(agent)
    assert cp1.status == "COMPLETED"
    ids1 = [r.invocation_id for r in cp1.syscall_log]

    # Second run — replay WITH the cached results. Fork at step=2
    # keeps all cached entries so the kernel replays both syscalls.
    forked2 = cp1.fork(at_step=2)
    cp2 = await kernel.run(agent, checkpoint=forked2)
    ids2 = [r.invocation_id for r in cp2.syscall_log]

    # IDs must match because the execution path is identical
    assert ids1 == ids2


@pytest.mark.asyncio
async def test_invocation_id_after_hitl_approve():
    """HITL-approved syscalls also get invocation_ids."""
    from castor import auto_approve

    received_operation_ids: list[str] = []

    async def dangerous(x: int = 0, operation_id: str = "") -> int:
        received_operation_ids.append(operation_id)
        return x

    kernel = Castor(tools=[dangerous], destructive=["dangerous"])

    async def agent(proxy):
        return await proxy.syscall("dangerous", {"x": 42})

    cp = await kernel.run_until_complete(agent, on_hitl=auto_approve)
    assert cp.status == "COMPLETED"
    assert len(cp.syscall_log) == 1
    assert cp.syscall_log[0].invocation_id is not None
    assert cp.syscall_log[0].was_hitl is True
    assert received_operation_ids == [cp.syscall_log[0].invocation_id]


@pytest.mark.asyncio
async def test_invocation_id_after_hitl_reject():
    """HITL-rejected syscalls also get invocation_ids."""
    from castor import auto_reject

    async def dangerous(x: int = 0) -> int:
        return x

    kernel = Castor(tools=[dangerous], destructive=["dangerous"])

    async def agent(proxy):
        result = await proxy.syscall("dangerous", {"x": 42})
        return result

    cp = await kernel.run_until_complete(agent, on_hitl=auto_reject)
    assert cp.status == "COMPLETED"
    assert len(cp.syscall_log) == 1
    assert cp.syscall_log[0].invocation_id is not None
    assert cp.syscall_log[0].was_hitl is True
