"""Tests for AgentRunner — the kernel executor."""

import asyncio

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner


@pytest.fixture
def registry():
    reg = ToolRegistry()

    @castor_tool(consumes="test", cost_per_use=1.0, registry=reg)
    def search(query: str) -> list:
        return [f"result for {query}"]

    @castor_tool(
        consumes="test",
        cost_per_use=1.0,
        destructive=True,
        requires_hitl=True,
        registry=reg,
    )
    def delete_files(paths: list[str]) -> int:
        return len(paths)

    return reg


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


@pytest.fixture
def runner(gate, cap_mgr):
    return AgentRunner(gate, cap_mgr)


def make_checkpoint(cap_mgr):
    caps = cap_mgr.create_capabilities({"test": 100.0})
    return AgentCheckpoint(
        pid="test-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )


class TestNormalCompletion:
    async def test_agent_completes(self, runner, cap_mgr):
        async def simple_agent(proxy: SyscallProxy) -> str:
            result = await proxy.syscall("search", {"query": "hello"})
            return f"done: {result}"

        checkpoint = make_checkpoint(cap_mgr)
        result = await runner.run(simple_agent, checkpoint)
        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 1

    async def test_agent_multiple_syscalls(self, runner, cap_mgr):
        async def multi_agent(proxy: SyscallProxy) -> str:
            r1 = await proxy.syscall("search", {"query": "a"})
            r2 = await proxy.syscall("search", {"query": "b"})
            return f"{r1} + {r2}"

        checkpoint = make_checkpoint(cap_mgr)
        result = await runner.run(multi_agent, checkpoint)
        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 2


class TestSuspension:
    async def test_hitl_suspension(self, runner, cap_mgr):
        async def destructive_agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("search", {"query": "files"})
            await proxy.syscall("delete_files", {"paths": ["/tmp/a"]})
            return "done"

        checkpoint = make_checkpoint(cap_mgr)
        result = await runner.run(destructive_agent, checkpoint)
        assert result.status == "SUSPENDED_FOR_HITL"
        assert result.pending_hitl is not None
        assert result.pending_hitl["tool_name"] == "delete_files"
        # Only the search syscall was completed
        assert len(result.syscall_log) == 1


class TestPreemption:
    async def test_preemption_via_task_cancel(self, runner, cap_mgr):
        started = asyncio.Event()

        async def slow_agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("search", {"query": "start"})
            started.set()
            # Simulate long-running work with an await point
            await asyncio.sleep(10)
            await proxy.syscall("search", {"query": "never_reached"})
            return "done"

        checkpoint = make_checkpoint(cap_mgr)
        task = await runner.run_as_task(slow_agent, checkpoint)

        # Wait for agent to start
        await started.wait()

        # Preempt
        runner.preempt("HUMAN_ABORT", {"instruction": "stop now"})

        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint.status == "PREEMPTED"
        assert checkpoint.preemption_reason == "HUMAN_ABORT"
        assert checkpoint.preemption_payload == {"instruction": "stop now"}
        # Only first syscall was completed
        assert len(checkpoint.syscall_log) == 1

    async def test_preemption_sets_context(self, runner, cap_mgr):
        started = asyncio.Event()

        async def agent(proxy: SyscallProxy) -> str:
            started.set()
            await asyncio.sleep(10)
            return "done"

        checkpoint = make_checkpoint(cap_mgr)
        task = await runner.run_as_task(agent, checkpoint)
        await started.wait()

        runner.preempt("BUDGET_EXHAUSTED")
        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint.preemption_reason == "BUDGET_EXHAUSTED"
        assert checkpoint.preemption_payload is None


class TestCheckpointConsistency:
    async def test_checkpoint_serializable_after_completion(self, runner, cap_mgr):
        async def agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("search", {"query": "test"})
            return "done"

        checkpoint = make_checkpoint(cap_mgr)
        await runner.run(agent, checkpoint)
        # Should serialize without error
        json_str = checkpoint.model_dump_json()
        loaded = AgentCheckpoint.model_validate_json(json_str)
        assert loaded.status == "COMPLETED"
        assert len(loaded.syscall_log) == 1

    async def test_checkpoint_serializable_after_suspension(self, runner, cap_mgr):
        async def agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("delete_files", {"paths": ["/tmp/x"]})
            return "done"

        checkpoint = make_checkpoint(cap_mgr)
        await runner.run(agent, checkpoint)
        json_str = checkpoint.model_dump_json()
        loaded = AgentCheckpoint.model_validate_json(json_str)
        assert loaded.status == "SUSPENDED_FOR_HITL"
        assert loaded.pending_hitl is not None
