"""Tests for CheckpointStore — SQLite persistence."""

import pytest

from castor.budget.manager import BudgetManager
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.db"
    return CheckpointStore(f"sqlite:///{db_path}")


@pytest.fixture
def budget_mgr():
    return BudgetManager()


def make_checkpoint(budget_mgr, pid="test-001"):
    caps = budget_mgr.create_budgets({"test": 100.0})
    return AgentCheckpoint(
        pid=pid,
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )


class TestSaveAndLoad:
    def test_save_and_load_roundtrip(self, store, budget_mgr):
        checkpoint = make_checkpoint(budget_mgr)
        store.save(checkpoint)
        loaded = store.load("test-001")
        assert loaded.pid == "test-001"
        assert loaded.status == "RUNNING"
        assert loaded.agent_function_name == "test_agent"
        assert loaded.capabilities["test"].max_budget == 100.0

    def test_save_with_syscall_log(self, store, budget_mgr):
        checkpoint = make_checkpoint(budget_mgr)
        checkpoint.syscall_log = [
            SyscallRecord(
                request={"tool_name": "search", "arguments": {"q": "hello"}},
                response=["result"],
            ),
            SyscallRecord(
                request={"tool_name": "fetch", "arguments": {"url": "http://x"}},
                response="content",
                was_hitl=True,
            ),
        ]
        store.save(checkpoint)
        loaded = store.load("test-001")
        assert len(loaded.syscall_log) == 2
        assert loaded.syscall_log[0].response == ["result"]
        assert loaded.syscall_log[1].was_hitl is True

    def test_save_with_preemption_context(self, store, budget_mgr):
        checkpoint = make_checkpoint(budget_mgr)
        checkpoint.status = "PREEMPTED"
        checkpoint.preemption_reason = "HUMAN_ABORT"
        checkpoint.preemption_payload = {"instruction": "stop"}
        store.save(checkpoint)
        loaded = store.load("test-001")
        assert loaded.preemption_reason == "HUMAN_ABORT"
        assert loaded.preemption_payload == {"instruction": "stop"}

    def test_save_with_nested_checkpoint(self, store, budget_mgr):
        child_budgets = budget_mgr.create_budgets({"test": 20.0})
        child = AgentCheckpoint(
            pid="child-001",
            parent_pid="test-001",
            status="COMPLETED",
            agent_function_name="child_agent",
            capabilities=child_budgets,
        )
        parent = make_checkpoint(budget_mgr)
        parent.syscall_log = [
            SyscallRecord(
                request={"tool_name": "spawn_agent", "arguments": {"role": "child"}},
                response="child result",
                child_checkpoint=child,
            )
        ]
        store.save(parent)
        loaded = store.load("test-001")
        assert loaded.syscall_log[0].child_checkpoint is not None
        assert loaded.syscall_log[0].child_checkpoint.pid == "child-001"

    def test_upsert_overwrites(self, store, budget_mgr):
        checkpoint = make_checkpoint(budget_mgr)
        store.save(checkpoint)
        checkpoint.status = "COMPLETED"
        store.save(checkpoint)
        loaded = store.load("test-001")
        assert loaded.status == "COMPLETED"


class TestLoadMissing:
    def test_load_nonexistent_raises(self, store):
        with pytest.raises(CheckpointNotFoundError, match="no-such-pid"):
            store.load("no-such-pid")


class TestDelete:
    def test_delete_removes_checkpoint(self, store, budget_mgr):
        checkpoint = make_checkpoint(budget_mgr)
        store.save(checkpoint)
        store.delete("test-001")
        with pytest.raises(CheckpointNotFoundError):
            store.load("test-001")

    def test_delete_nonexistent_is_noop(self, store):
        store.delete("no-such-pid")  # Should not raise


class TestListPids:
    def test_list_empty(self, store):
        assert store.list_pids() == []

    def test_list_multiple(self, store, budget_mgr):
        for pid in ["a", "b", "c"]:
            store.save(make_checkpoint(budget_mgr, pid=pid))
        pids = store.list_pids()
        assert set(pids) == {"a", "b", "c"}


class TestParentPidQuery:
    def test_list_by_parent(self, store, budget_mgr):
        """List all checkpoints with a given parent_pid."""
        parent = make_checkpoint(budget_mgr, pid="parent-001")
        store.save(parent)

        child1 = AgentCheckpoint(
            pid="parent-001::child-0",
            parent_pid="parent-001",
            status="RUNNING",
            agent_function_name="child",
            capabilities=budget_mgr.create_budgets({"test": 10.0}),
        )
        child2 = AgentCheckpoint(
            pid="parent-001::child-1",
            parent_pid="parent-001",
            status="COMPLETED",
            agent_function_name="child",
            capabilities=budget_mgr.create_budgets({"test": 10.0}),
        )
        store.save(child1)
        store.save(child2)

        children = store.list_by_parent("parent-001")
        assert set(c.pid for c in children) == {
            "parent-001::child-0",
            "parent-001::child-1",
        }

    def test_list_by_parent_empty(self, store):
        assert store.list_by_parent("no-parent") == []


class TestGCOrphans:
    def test_gc_marks_orphaned_children(self, store, budget_mgr):
        """Children of completed parents with RUNNING status become FAILED."""
        parent = make_checkpoint(budget_mgr, pid="parent-001")
        parent.status = "COMPLETED"
        store.save(parent)

        child = AgentCheckpoint(
            pid="parent-001::child-0",
            parent_pid="parent-001",
            status="RUNNING",
            agent_function_name="child",
            capabilities=budget_mgr.create_budgets({"test": 10.0}),
        )
        store.save(child)

        orphaned = store.gc_orphans()
        assert len(orphaned) == 1
        assert orphaned[0] == "parent-001::child-0"

        reloaded = store.load("parent-001::child-0")
        assert reloaded.status == "FAILED"


class TestWAL:
    def test_write_wal_entry(self, store, budget_mgr):
        """WAL entry can be written and read back."""
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "hello"},
            budget_snapshot={"test": 99.0},
        )
        entries = store.list_pending_wal()
        assert len(entries) == 1
        assert entries[0]["pid"] == "test-001"
        assert entries[0]["status"] == "PENDING"

    def test_complete_wal_entry(self, store, budget_mgr):
        """Completing a WAL entry marks it COMPLETED with result."""
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "hello"},
            budget_snapshot={"test": 99.0},
        )
        store.complete_wal(pid="test-001", syscall_index=0, result=["found"])
        entries = store.list_pending_wal()
        assert len(entries) == 0

    def test_recover_refunds_pending_wal(self, store, budget_mgr):
        """Recovery refunds budget for PENDING WAL entries and marks ABANDONED."""
        checkpoint = make_checkpoint(budget_mgr)
        checkpoint.capabilities["test"].current_usage = 1.0
        store.save(checkpoint)
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "hello"},
            budget_snapshot={"test": 0.0},
        )
        recovered = store.recover("test-001")
        assert recovered is not None
        assert recovered.capabilities["test"].current_usage == 0.0

    def test_recover_no_pending_returns_none(self, store, budget_mgr):
        """Recovery returns None when no PENDING WAL entries exist."""
        checkpoint = make_checkpoint(budget_mgr)
        store.save(checkpoint)
        assert store.recover("test-001") is None

    def test_gc_completed_wal(self, store, budget_mgr):
        """GC removes COMPLETED and ABANDONED WAL entries."""
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "a"},
            budget_snapshot={},
        )
        store.complete_wal(pid="test-001", syscall_index=0, result="ok")
        store.gc_wal()
        assert store.list_pending_wal() == []
