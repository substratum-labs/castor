"""Tests for CheckpointStore — SQLite persistence."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.stream.persistence import CheckpointNotFoundError, CheckpointStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.db"
    return CheckpointStore(f"sqlite:///{db_path}")


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


def make_checkpoint(cap_mgr, pid="test-001"):
    caps = cap_mgr.create_capabilities({"test": 100.0})
    return AgentCheckpoint(
        pid=pid,
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )


class TestSaveAndLoad:
    def test_save_and_load_roundtrip(self, store, cap_mgr):
        checkpoint = make_checkpoint(cap_mgr)
        store.save(checkpoint)
        loaded = store.load("test-001")
        assert loaded.pid == "test-001"
        assert loaded.status == "RUNNING"
        assert loaded.agent_function_name == "test_agent"
        assert loaded.capabilities["test"].max_budget == 100.0

    def test_save_with_syscall_log(self, store, cap_mgr):
        checkpoint = make_checkpoint(cap_mgr)
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

    def test_save_with_preemption_context(self, store, cap_mgr):
        checkpoint = make_checkpoint(cap_mgr)
        checkpoint.status = "PREEMPTED"
        checkpoint.preemption_reason = "HUMAN_ABORT"
        checkpoint.preemption_payload = {"instruction": "stop"}
        store.save(checkpoint)
        loaded = store.load("test-001")
        assert loaded.preemption_reason == "HUMAN_ABORT"
        assert loaded.preemption_payload == {"instruction": "stop"}

    def test_save_with_nested_checkpoint(self, store, cap_mgr):
        child_caps = cap_mgr.create_capabilities({"test": 20.0})
        child = AgentCheckpoint(
            pid="child-001",
            parent_pid="test-001",
            status="COMPLETED",
            agent_function_name="child_agent",
            capabilities=child_caps,
        )
        parent = make_checkpoint(cap_mgr)
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

    def test_upsert_overwrites(self, store, cap_mgr):
        checkpoint = make_checkpoint(cap_mgr)
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
    def test_delete_removes_checkpoint(self, store, cap_mgr):
        checkpoint = make_checkpoint(cap_mgr)
        store.save(checkpoint)
        store.delete("test-001")
        with pytest.raises(CheckpointNotFoundError):
            store.load("test-001")

    def test_delete_nonexistent_is_noop(self, store):
        store.delete("no-such-pid")  # Should not raise


class TestListPids:
    def test_list_empty(self, store):
        assert store.list_pids() == []

    def test_list_multiple(self, store, cap_mgr):
        for pid in ["a", "b", "c"]:
            store.save(make_checkpoint(cap_mgr, pid=pid))
        pids = store.list_pids()
        assert set(pids) == {"a", "b", "c"}


class TestWAL:
    def test_write_wal_entry(self, store, cap_mgr):
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

    def test_complete_wal_entry(self, store, cap_mgr):
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

    def test_recover_refunds_pending_wal(self, store, cap_mgr):
        """Recovery refunds budget for PENDING WAL entries and marks ABANDONED."""
        checkpoint = make_checkpoint(cap_mgr)
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

    def test_recover_no_pending_returns_none(self, store, cap_mgr):
        """Recovery returns None when no PENDING WAL entries exist."""
        checkpoint = make_checkpoint(cap_mgr)
        store.save(checkpoint)
        assert store.recover("test-001") is None

    def test_gc_completed_wal(self, store, cap_mgr):
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
