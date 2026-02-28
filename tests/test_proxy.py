"""Tests for SyscallProxy — the replay gateway."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.dam.decorator import castor_tool
from castor.dam.registry import ToolRegistry
from castor.dam.validator import CastorDam
from castor.models.checkpoint import (
    AgentCheckpoint,
    SuspendInterrupt,
    SyscallRecord,
)
from castor.stream.proxy import ReplayDivergenceError, SyscallProxy


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def dam(registry):
    return CastorDam(registry)


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


def make_checkpoint(cap_mgr, caps=None, syscall_log=None):
    """Create a checkpoint with default capabilities."""
    if caps is None:
        caps = cap_mgr.create_capabilities({"test": 100.0})
    return AgentCheckpoint(
        pid="test-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
        syscall_log=syscall_log or [],
    )


def register_search(registry):
    @castor_tool(consumes="test", cost_per_use=1.0, registry=registry)
    def search(query: str) -> list:
        return [f"result for {query}"]

    return search


def register_delete(registry):
    @castor_tool(
        consumes="test",
        cost_per_use=1.0,
        destructive=True,
        requires_hitl=True,
        registry=registry,
    )
    def delete_files(paths: list[str]) -> int:
        return len(paths)

    return delete_files


class TestReplayPath:
    async def test_replay_returns_cached_response(self, registry, dam, cap_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(
            cap_mgr,
            syscall_log=[
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "hello"}},
                    response=["cached result"],
                )
            ],
        )
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["cached result"]
        assert proxy._replay_index == 1

    async def test_replay_multiple_cached(self, registry, dam, cap_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(
            cap_mgr,
            syscall_log=[
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "a"}},
                    response=["result a"],
                ),
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "b"}},
                    response=["result b"],
                ),
            ],
        )
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        r1 = await proxy.syscall("search", {"query": "a"})
        r2 = await proxy.syscall("search", {"query": "b"})
        assert r1 == ["result a"]
        assert r2 == ["result b"]
        assert not proxy.is_replaying

    async def test_replay_divergence_raises(self, registry, dam, cap_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(
            cap_mgr,
            syscall_log=[
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "hello"}},
                    response=["cached"],
                )
            ],
        )
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        with pytest.raises(ReplayDivergenceError) as exc_info:
            await proxy.syscall("search", {"query": "DIFFERENT"})
        assert exc_info.value.index == 0


class TestFastPath:
    async def test_new_syscall_executes_and_logs(self, registry, dam, cap_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["result for hello"]
        assert len(checkpoint.syscall_log) == 1
        assert checkpoint.syscall_log[0].request == {
            "tool_name": "search",
            "arguments": {"query": "hello"},
        }
        assert checkpoint.syscall_log[0].response == ["result for hello"]

    async def test_capability_deducted(self, registry, dam, cap_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        await proxy.syscall("search", {"query": "test"})
        assert checkpoint.capabilities["test"].current_usage == 1.0

    async def test_capability_exhaustion(self, registry, dam, cap_mgr):
        register_search(registry)
        caps = cap_mgr.create_capabilities({"test": 0.5})  # Not enough for cost=1.0
        checkpoint = make_checkpoint(cap_mgr, caps=caps)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        result = await proxy.syscall("search", {"query": "test"})
        assert result["status"] == "INSUFFICIENT_CAPABILITY"
        assert len(checkpoint.syscall_log) == 1


class TestSlowPath:
    async def test_destructive_tool_suspends(self, registry, dam, cap_mgr):
        register_delete(registry)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        with pytest.raises(SuspendInterrupt) as exc_info:
            await proxy.syscall("delete_files", {"paths": ["/tmp/a"]})

        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert checkpoint.pending_hitl == {
            "tool_name": "delete_files",
            "arguments": {"paths": ["/tmp/a"]},
        }
        assert exc_info.value.checkpoint is checkpoint

    async def test_suspension_does_not_log(self, registry, dam, cap_mgr):
        register_delete(registry)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall("delete_files", {"paths": ["/tmp/a"]})

        # The suspended syscall is NOT in the log
        assert len(checkpoint.syscall_log) == 0


class TestValidationError:
    async def test_invalid_args_return_feedback(self, registry, dam, cap_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        # 'query' is required but missing
        result = await proxy.syscall("search", {})
        assert result["status"] == "VALIDATION_ERROR"
        assert "query" in result["feedback_message"]
        # Logged so replay can serve it
        assert len(checkpoint.syscall_log) == 1


class TestReplayThenLive:
    async def test_replay_then_live_execution(self, registry, dam, cap_mgr):
        """After replaying cached syscalls, new ones execute live."""
        register_search(registry)
        checkpoint = make_checkpoint(
            cap_mgr,
            syscall_log=[
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "cached"}},
                    response=["old result"],
                )
            ],
        )
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        # Replay
        r1 = await proxy.syscall("search", {"query": "cached"})
        assert r1 == ["old result"]

        # Live
        r2 = await proxy.syscall("search", {"query": "new"})
        assert r2 == ["result for new"]
        assert len(checkpoint.syscall_log) == 2
