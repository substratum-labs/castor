"""Tests for castor.cli HITL commands — reject, modify."""

import pytest

from castor.budget.manager import BudgetManager
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.persistence import CheckpointStore


@pytest.fixture()
def store(tmp_path):
    db_path = tmp_path / "test.db"
    return CheckpointStore(f"sqlite:///{db_path}")


@pytest.fixture()
def budget_mgr():
    return BudgetManager()


@pytest.fixture()
def hitl_checkpoint(store, budget_mgr):
    cp = AgentCheckpoint(
        pid="agent-hitl-1",
        status="SUSPENDED_FOR_HITL",
        agent_function_name="test_agent",
        capabilities=budget_mgr.create_budgets({"api": 10.0}),
        pending_hitl={"tool_name": "dangerous_tool", "arguments": {"x": 1}},
    )
    store.save(cp)
    return cp


def test_cmd_reject(store, hitl_checkpoint, capsys):
    from castor.cli.hitl import cmd_reject

    cmd_reject(store, "agent-hitl-1", "too dangerous")
    output = capsys.readouterr().out
    assert "Rejected" in output

    # Verify checkpoint was updated
    cp = store.load("agent-hitl-1")
    assert cp.pending_hitl is None


def test_cmd_reject_no_hitl(store, budget_mgr):
    cp = AgentCheckpoint(
        pid="agent-no-hitl",
        status="COMPLETED",
        agent_function_name="test",
        capabilities={},
    )
    store.save(cp)

    from castor.cli.hitl import cmd_reject

    with pytest.raises(SystemExit):
        cmd_reject(store, "agent-no-hitl", "reason")


def test_cmd_modify(store, hitl_checkpoint, capsys):
    from castor.cli.hitl import cmd_modify

    cmd_modify(store, "agent-hitl-1", "use safer params")
    output = capsys.readouterr().out
    assert "Modified" in output


def test_cmd_reject_not_found(store):
    from castor.cli.hitl import cmd_reject

    with pytest.raises(SystemExit):
        cmd_reject(store, "nonexistent", "reason")
