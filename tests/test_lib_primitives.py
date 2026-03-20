"""Tests for castor.lib.primitives — tool, chat, budget, try_tool."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.lib._context import set_proxy
from castor.models.checkpoint import AgentCheckpoint
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
def proxy(gate, cap_mgr):
    cp = AgentCheckpoint(
        pid="test-prim-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)
    return p


@pytest.mark.asyncio()
async def test_tool(proxy):
    from castor.lib import tool

    result = await tool("search", query="hello")
    assert result == "results for hello"


@pytest.mark.asyncio()
async def test_try_tool(proxy):
    from castor.lib import try_tool

    result = await try_tool("search", query="hello")
    assert result == "results for hello"


def test_budget(proxy):
    from castor.lib import budget

    remaining = budget("api")
    assert remaining == 10.0


@pytest.mark.asyncio()
async def test_chat_calls_tool(proxy, registry, gate):
    """chat() delegates to the named LLM tool."""

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        return f"LLM says: {prompt}"

    registry.register(llm_inference._castor_metadata)

    from castor.lib import chat

    result = await chat("summarize this")
    assert result == "LLM says: summarize this"
