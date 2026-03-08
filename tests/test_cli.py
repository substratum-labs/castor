"""Tests for Castor CLI."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from castor.capability.manager import CapabilityManager
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.persistence import CheckpointStore


@pytest.fixture
def temp_db():
    """Create a temporary database path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture
def store(temp_db):
    return CheckpointStore(f"sqlite:///{temp_db}")


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


def run_cli(*args: str, db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the castor CLI and return the result."""
    cmd = ["uv", "run", "castor", "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def make_suspended(cap_mgr, pid="agent-001"):
    caps = cap_mgr.create_capabilities({"disk": 50.0})
    return AgentCheckpoint(
        pid=pid,
        status="SUSPENDED_FOR_HITL",
        agent_function_name="test_agent",
        capabilities=caps,
        pending_hitl={
            "tool_name": "delete_files",
            "arguments": {"paths": ["/tmp/a"]},
        },
    )


class TestCLIList:
    def test_list_empty(self, temp_db):
        result = run_cli("list", db_path=temp_db)
        assert result.returncode == 0
        assert "No checkpoints" in result.stdout

    def test_list_shows_checkpoints(self, store, cap_mgr, temp_db):
        cp1 = make_suspended(cap_mgr, pid="agent-001")
        caps2 = cap_mgr.create_capabilities({"disk": 10.0})
        cp2 = AgentCheckpoint(
            pid="agent-002",
            status="COMPLETED",
            agent_function_name="other",
            capabilities=caps2,
        )
        store.save(cp1)
        store.save(cp2)

        result = run_cli("list", db_path=temp_db)
        assert result.returncode == 0
        assert "agent-001" in result.stdout
        assert "agent-002" in result.stdout
        assert "HITL" in result.stdout
        assert "DONE" in result.stdout


class TestCLIShow:
    def test_show_checkpoint(self, store, cap_mgr, temp_db):
        cp = make_suspended(cap_mgr)
        store.save(cp)

        result = run_cli("show", "agent-001", db_path=temp_db)
        assert result.returncode == 0
        assert "agent-001" in result.stdout
        assert "SUSPENDED_FOR_HITL" in result.stdout
        assert "test_agent" in result.stdout
        assert "delete_files" in result.stdout
        assert "disk" in result.stdout

    def test_show_missing_pid(self, temp_db):
        result = run_cli("show", "ghost-999", db_path=temp_db)
        assert result.returncode == 1
        assert "not found" in result.stderr


class TestCLIReject:
    def test_reject_records_feedback(self, store, cap_mgr, temp_db):
        cp = make_suspended(cap_mgr)
        store.save(cp)

        result = run_cli(
            "reject",
            "agent-001",
            "--feedback",
            "Too dangerous",
            db_path=temp_db,
        )
        assert result.returncode == 0
        assert "Rejected" in result.stdout

        # Verify checkpoint was updated in DB
        updated = store.load("agent-001")
        assert updated.status == "RUNNING"
        assert updated.pending_hitl is None
        assert len(updated.syscall_log) == 1
        assert updated.syscall_log[0].response["status"] == "HITL_REJECTED"
        assert updated.syscall_log[0].response["human_feedback"] == "Too dangerous"

    def test_reject_child_hitl_blocked(self, store, cap_mgr, temp_db):
        caps = cap_mgr.create_capabilities({"disk": 50.0})
        cp = AgentCheckpoint(
            pid="agent-001",
            status="SUSPENDED_FOR_HITL",
            agent_function_name="test_agent",
            capabilities=caps,
            pending_hitl={
                "tool_name": "join_agent",
                "arguments": {"handle": "agent-001::child-0"},
                "child_pid": "agent-001::child-0",
            },
        )
        store.save(cp)

        result = run_cli(
            "reject",
            "agent-001",
            "--feedback",
            "nope",
            db_path=temp_db,
        )
        assert result.returncode == 1
        assert "child HITL" in result.stderr

    def test_reject_no_pending_fails(self, store, cap_mgr, temp_db):
        caps = cap_mgr.create_capabilities({"disk": 10.0})
        cp = AgentCheckpoint(
            pid="agent-001",
            status="RUNNING",
            agent_function_name="test",
            capabilities=caps,
        )
        store.save(cp)

        result = run_cli(
            "reject",
            "agent-001",
            "--feedback",
            "nope",
            db_path=temp_db,
        )
        assert result.returncode == 1
        assert "no pending HITL" in result.stderr

    def test_reject_missing_pid(self, temp_db):
        result = run_cli(
            "reject",
            "ghost",
            "--feedback",
            "nope",
            db_path=temp_db,
        )
        assert result.returncode == 1
        assert "not found" in result.stderr


class TestCLIModify:
    def test_modify_records_feedback(self, store, cap_mgr, temp_db):
        cp = make_suspended(cap_mgr)
        store.save(cp)

        result = run_cli(
            "modify",
            "agent-001",
            "--feedback",
            "Only delete old files",
            db_path=temp_db,
        )
        assert result.returncode == 0
        assert "Modified" in result.stdout

        updated = store.load("agent-001")
        assert updated.status == "RUNNING"
        assert updated.pending_hitl is None
        assert len(updated.syscall_log) == 1
        assert updated.syscall_log[0].response["status"] == "HITL_MODIFIED"
        assert "old files" in updated.syscall_log[0].response["human_feedback"]

    def test_modify_child_hitl_blocked(self, store, cap_mgr, temp_db):
        caps = cap_mgr.create_capabilities({"disk": 50.0})
        cp = AgentCheckpoint(
            pid="agent-001",
            status="SUSPENDED_FOR_HITL",
            agent_function_name="test_agent",
            capabilities=caps,
            pending_hitl={
                "tool_name": "spawn_agent",
                "arguments": {"agent_name": "child", "capabilities": {}},
                "child_pid": "agent-001::child-0",
            },
        )
        store.save(cp)

        result = run_cli(
            "modify",
            "agent-001",
            "--feedback",
            "change it",
            db_path=temp_db,
        )
        assert result.returncode == 1
        assert "child HITL" in result.stderr

    def test_modify_no_pending_fails(self, store, cap_mgr, temp_db):
        caps = cap_mgr.create_capabilities({"disk": 10.0})
        cp = AgentCheckpoint(
            pid="agent-001",
            status="COMPLETED",
            agent_function_name="test",
            capabilities=caps,
        )
        store.save(cp)

        result = run_cli(
            "modify",
            "agent-001",
            "--feedback",
            "change it",
            db_path=temp_db,
        )
        assert result.returncode == 1
        assert "no pending HITL" in result.stderr
