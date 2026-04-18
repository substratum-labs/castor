"""Tests for castor.lib._context — ContextVar bridge."""

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.lib._context import get_proxy, set_proxy


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def budget_mgr():
    return BudgetManager()


def test_get_proxy_outside_run_raises():
    """get_proxy() raises RuntimeError when no proxy is set."""
    with pytest.raises(RuntimeError, match="must be called inside"):
        get_proxy()


def test_set_and_get_proxy(gate, budget_mgr):
    """set_proxy() makes the proxy available via get_proxy()."""
    from castor.models.checkpoint import AgentCheckpoint
    from castor.scheduler.proxy import SyscallProxy

    cp = AgentCheckpoint(
        pid="test-ctx-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=budget_mgr.create_budgets({"test": 100.0}),
    )
    proxy = SyscallProxy(cp, gate, budget_mgr)
    set_proxy(proxy)
    assert get_proxy() is proxy
