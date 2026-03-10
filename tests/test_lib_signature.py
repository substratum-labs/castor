"""Tests for dual-signature detection in AgentRunner / Castor.run()."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.runner import AgentRunner


@pytest.fixture()
def registry():
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    reg.register(search._castor_metadata)
    return reg


@pytest.fixture()
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def runner(gate, cap_mgr):
    return AgentRunner(gate, cap_mgr)


@pytest.mark.asyncio()
async def test_legacy_agent_with_proxy_param(runner, cap_mgr):
    """Legacy agent (1 param) still works."""

    async def my_agent(proxy):
        result = await proxy.syscall("search", query="test")
        return result

    cp = AgentCheckpoint(
        pid="sig-legacy",
        status="RUNNING",
        agent_function_name="my_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    cp = await runner.run(my_agent, cp)
    assert cp.status == "COMPLETED"
    assert cp.result == "results for test"


@pytest.mark.asyncio()
async def test_new_style_agent_no_params(runner, cap_mgr):
    """New-style agent (0 params) uses castor.lib via ContextVar."""
    from castor.lib import tool

    async def my_agent():
        return await tool("search", query="test")

    cp = AgentCheckpoint(
        pid="sig-new",
        status="RUNNING",
        agent_function_name="my_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    cp = await runner.run(my_agent, cp)
    assert cp.status == "COMPLETED"
    assert cp.result == "results for test"


@pytest.mark.asyncio()
async def test_contextvar_set_for_legacy_agent(runner, cap_mgr):
    """ContextVar is set even for legacy agents — enables gradual migration."""
    from castor.lib import budget

    async def my_agent(proxy):
        remaining = budget("api")
        return remaining

    cp = AgentCheckpoint(
        pid="sig-mixed",
        status="RUNNING",
        agent_function_name="my_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    cp = await runner.run(my_agent, cp)
    assert cp.status == "COMPLETED"
    assert cp.result == 10.0
