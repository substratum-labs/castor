"""Tests for castor.cli process commands — ps, inspect."""

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
def saved_checkpoint(store, budget_mgr):
    cp = AgentCheckpoint(
        pid="agent-test-1234",
        status="COMPLETED",
        agent_function_name="test_agent",
        capabilities=budget_mgr.create_budgets({"api": 10.0}),
        result="done",
    )
    store.save(cp)
    return cp


def test_cmd_ps(store, saved_checkpoint, capsys):
    from castor.cli.process import cmd_ps

    cmd_ps(store)
    output = capsys.readouterr().out
    assert "agent-test-1234" in output
    assert "DONE" in output


def test_cmd_ps_empty(store, capsys):
    from castor.cli.process import cmd_ps

    cmd_ps(store)
    output = capsys.readouterr().out
    assert "No checkpoints" in output or "No agents" in output


def test_cmd_inspect(store, saved_checkpoint, capsys):
    from castor.cli.process import cmd_inspect

    cmd_inspect(store, "agent-test-1234")
    output = capsys.readouterr().out
    assert "agent-test-1234" in output
    assert "COMPLETED" in output
    assert "test_agent" in output


def test_cmd_inspect_not_found(store):
    from castor.cli.process import cmd_inspect

    with pytest.raises(SystemExit):
        cmd_inspect(store, "nonexistent")
