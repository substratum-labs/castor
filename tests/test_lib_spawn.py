"""Tests for castor.lib.spawn — spawn and join."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.lib._context import set_proxy
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.agent_registry import AgentRegistry, castor_agent
from castor.scheduler.proxy import SyscallProxy


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
def agent_reg():
    return AgentRegistry()


@pytest.fixture()
def proxy(gate, cap_mgr, agent_reg):
    cp = AgentCheckpoint(
        pid="test-spawn-1",
        status="RUNNING",
        agent_function_name="parent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    p = SyscallProxy(
        cp, gate, cap_mgr, agent_registry=agent_reg
    )
    set_proxy(p)
    return p


@pytest.mark.asyncio()
async def test_spawn_and_join(proxy, agent_reg):
    from castor.lib import join, spawn

    @castor_agent(registry=agent_reg)
    async def child_agent(p: SyscallProxy) -> str:
        return "child done"

    handle = await spawn("child_agent", capabilities={"api": 2.0})
    assert isinstance(handle, str)
    result = await join(handle)
    assert result == "child done"
