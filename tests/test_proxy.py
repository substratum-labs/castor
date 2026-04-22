"""Tests for SyscallProxy — the replay gateway."""

import asyncio

import pytest

from castor.budget.manager import BudgetExhaustedError, BudgetManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import (
    AgentCheckpoint,
    SuspendInterrupt,
    SyscallRecord,
)
from castor.scheduler.proxy import ReplayDivergenceError, SyscallProxy


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def budget_mgr():
    return BudgetManager()


def make_checkpoint(budget_mgr, caps=None, syscall_log=None):
    """Create a checkpoint with default capabilities."""
    if caps is None:
        caps = budget_mgr.create_budgets({"test": 100.0})
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
    async def test_replay_returns_cached_response(self, registry, gate, budget_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(
            budget_mgr,
            syscall_log=[
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "hello"}},
                    response=["cached result"],
                )
            ],
        )
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["cached result"]
        assert proxy._replay_index == 1

    async def test_replay_multiple_cached(self, registry, gate, budget_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(
            budget_mgr,
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
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        r1 = await proxy.syscall("search", {"query": "a"})
        r2 = await proxy.syscall("search", {"query": "b"})
        assert r1 == ["result a"]
        assert r2 == ["result b"]
        assert not proxy.is_replaying

    async def test_replay_divergence_raises(self, registry, gate, budget_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(
            budget_mgr,
            syscall_log=[
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "hello"}},
                    response=["cached"],
                )
            ],
        )
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        with pytest.raises(ReplayDivergenceError) as exc_info:
            await proxy.syscall("search", {"query": "DIFFERENT"})
        assert exc_info.value.index == 0


class TestFastPath:
    async def test_new_syscall_executes_and_logs(self, registry, gate, budget_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["result for hello"]
        assert len(checkpoint.syscall_log) == 1
        assert checkpoint.syscall_log[0].request == {
            "tool_name": "search",
            "arguments": {"query": "hello"},
        }
        assert checkpoint.syscall_log[0].response == ["result for hello"]

    async def test_capability_deducted(self, registry, gate, budget_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        await proxy.syscall("search", {"query": "test"})
        assert checkpoint.capabilities["test"].current_usage == 1.0

    async def test_capability_exhaustion(self, registry, gate, budget_mgr):
        register_search(registry)
        caps = budget_mgr.create_budgets({"test": 0.5})  # Not enough for cost=1.0
        checkpoint = make_checkpoint(budget_mgr, caps=caps)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        with pytest.raises(BudgetExhaustedError):
            await proxy.syscall("search", {"query": "test"})
        assert len(checkpoint.syscall_log) == 1
        assert checkpoint.status == "BUDGET_EXHAUSTED"


class TestSlowPath:
    async def test_destructive_tool_suspends(self, registry, gate, budget_mgr):
        register_delete(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        with pytest.raises(SuspendInterrupt) as exc_info:
            await proxy.syscall("delete_files", {"paths": ["/tmp/a"]})

        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert checkpoint.pending_hitl == {
            "tool_name": "delete_files",
            "arguments": {"paths": ["/tmp/a"]},
        }
        assert exc_info.value.checkpoint is checkpoint

    async def test_suspension_does_not_log(self, registry, gate, budget_mgr):
        register_delete(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall("delete_files", {"paths": ["/tmp/a"]})

        # The suspended syscall is NOT in the log
        assert len(checkpoint.syscall_log) == 0


class TestValidationError:
    async def test_invalid_args_return_feedback(self, registry, gate, budget_mgr):
        register_search(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        # 'query' is required but missing
        result = await proxy.syscall("search", {})
        assert result["status"] == "VALIDATION_ERROR"
        assert "query" in result["feedback_message"]
        # Logged so replay can serve it
        assert len(checkpoint.syscall_log) == 1


class TestBudgetRefundOnFailure:
    """Budget must be refunded if tool execution fails or is cancelled."""

    async def test_refund_on_tool_exception(self, registry, gate, budget_mgr):
        @castor_tool(consumes="test", cost_per_use=2.0, registry=registry)
        async def flaky_tool(query: str) -> str:
            raise RuntimeError("network timeout")

        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        with pytest.raises(RuntimeError, match="network timeout"):
            await proxy.syscall("flaky_tool", {"query": "test"})

        # Budget must be fully refunded — no record logged, no leak
        assert checkpoint.capabilities["test"].current_usage == 0.0
        assert len(checkpoint.syscall_log) == 0

    async def test_refund_on_cancellation(self, registry, gate, budget_mgr):
        """Simulates preemption via asyncio.CancelledError during tool exec."""
        cancel_event = asyncio.Event()

        @castor_tool(consumes="test", cost_per_use=5.0, registry=registry)
        async def slow_tool(query: str) -> str:
            cancel_event.set()
            await asyncio.sleep(60)  # will be cancelled
            return "never reached"

        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        async def run_syscall():
            await proxy.syscall("slow_tool", {"query": "test"})

        task = asyncio.create_task(run_syscall())
        await cancel_event.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Budget must be fully refunded after cancellation
        assert checkpoint.capabilities["test"].current_usage == 0.0
        assert len(checkpoint.syscall_log) == 0

    async def test_no_refund_on_success(self, registry, gate, budget_mgr):
        """Sanity check: successful execution keeps the deduction."""
        register_search(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        await proxy.syscall("search", {"query": "hello"})

        assert checkpoint.capabilities["test"].current_usage == 1.0
        assert len(checkpoint.syscall_log) == 1


class TestWALIntegration:
    @pytest.fixture
    def store(self, tmp_path):
        from castor.scheduler.persistence import CheckpointStore

        return CheckpointStore(f"sqlite:///{tmp_path / 'test.db'}")

    async def test_wal_written_before_execution(
        self, registry, gate, budget_mgr, store
    ):
        """WAL entry is written before tool executes."""
        register_search(registry)
        checkpoint = make_checkpoint(budget_mgr)
        store.save(checkpoint)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr, checkpoint_store=store)

        await proxy.syscall("search", {"query": "hello"})

        # WAL should be completed (no pending entries)
        assert store.list_pending_wal() == []

    async def test_wal_abandoned_on_failure(self, registry, gate, budget_mgr, store):
        """If tool execution fails, WAL entry is marked ABANDONED (not left PENDING)."""

        @castor_tool(consumes="test", cost_per_use=2.0, registry=registry)
        async def failing_tool(query: str) -> str:
            raise RuntimeError("boom")

        checkpoint = make_checkpoint(budget_mgr)
        store.save(checkpoint)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr, checkpoint_store=store)

        with pytest.raises(RuntimeError, match="boom"):
            await proxy.syscall("failing_tool", {"query": "test"})

        # WAL entry should be ABANDONED, not left PENDING (prevents double refund)
        assert store.list_pending_wal() == []

    async def test_no_store_no_wal(self, registry, gate, budget_mgr):
        """When no store is provided, proxy works without WAL (backwards compat)."""
        register_search(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)  # no store

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["result for hello"]


class TestToolTimeout:
    async def test_async_tool_timeout(self, registry, gate, budget_mgr):
        """Async tool exceeding timeout raises asyncio.TimeoutError, budget refunded."""

        @castor_tool(
            consumes="test",
            cost_per_use=1.0,
            timeout_seconds=0.1,
            registry=registry,
        )
        async def slow_tool(query: str) -> str:
            await asyncio.sleep(10)
            return "never reached"

        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        with pytest.raises(asyncio.TimeoutError):
            await proxy.syscall("slow_tool", {"query": "test"})

        # Budget refunded
        assert checkpoint.capabilities["test"].current_usage == 0.0

    async def test_sync_tool_timeout(self, registry, gate, budget_mgr):
        """Sync CPU-bound tool with timeout runs in executor and times out."""
        import time

        @castor_tool(
            consumes="test",
            cost_per_use=1.0,
            timeout_seconds=0.1,
            registry=registry,
        )
        def cpu_bound_tool(query: str) -> str:
            time.sleep(10)
            return "never reached"

        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        with pytest.raises(asyncio.TimeoutError):
            await proxy.syscall("cpu_bound_tool", {"query": "test"})

        assert checkpoint.capabilities["test"].current_usage == 0.0

    async def test_no_timeout_default(self, registry, gate, budget_mgr):
        """Tools without timeout_seconds work normally (backwards compat)."""
        register_search(registry)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["result for hello"]


class TestReplayThenLive:
    async def test_replay_then_live_execution(self, registry, gate, budget_mgr):
        """After replaying cached syscalls, new ones execute live."""
        register_search(registry)
        checkpoint = make_checkpoint(
            budget_mgr,
            syscall_log=[
                SyscallRecord(
                    request={"tool_name": "search", "arguments": {"query": "cached"}},
                    response=["old result"],
                )
            ],
        )
        proxy = SyscallProxy(checkpoint, gate, budget_mgr)

        # Replay
        r1 = await proxy.syscall("search", {"query": "cached"})
        assert r1 == ["old result"]

        # Live
        r2 = await proxy.syscall("search", {"query": "new"})
        assert r2 == ["result for new"]
        assert len(checkpoint.syscall_log) == 2
