"""Tests for the Castor facade API and SyscallProxy enhancements."""

import pytest

from castor import (
    AgentCheckpoint,
    CapabilityManager,
    CastorDam,
    SyscallProxy,
    castor_tool,
)
from castor.dam.registry import ToolRegistry

# ── Fixtures ──


@pytest.fixture()
def registry():
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
    async def search(query: str) -> list[str]:
        return [f"Result: {query}"]

    @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
    async def add(a: int, b: int) -> int:
        return a + b

    return reg


@pytest.fixture()
def dam(registry):
    return CastorDam(registry)


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def checkpoint(cap_mgr):
    caps = cap_mgr.create_capabilities({"api": 100.0})
    return AgentCheckpoint(
        pid="test-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )


@pytest.fixture()
def proxy(checkpoint, dam, cap_mgr):
    return SyscallProxy(checkpoint, dam, cap_mgr)


# ── Task 1: syscall kwargs ──


class TestSyscallKwargs:
    @pytest.mark.asyncio
    async def test_syscall_with_kwargs(self, proxy):
        """syscall() accepts keyword arguments instead of a dict."""
        result = await proxy.syscall("search", query="hello")
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_syscall_with_dict_still_works(self, proxy):
        """syscall() still accepts a dict (backward compat)."""
        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_syscall_kwargs_multiple_args(self, proxy):
        """syscall() kwargs works with multiple parameters."""
        result = await proxy.syscall("add", a=2, b=3)
        assert result == 5

    @pytest.mark.asyncio
    async def test_syscall_rejects_dict_and_kwargs(self, proxy):
        """syscall() raises if both positional dict and kwargs given."""
        with pytest.raises(TypeError, match="Cannot pass both"):
            await proxy.syscall("search", {"query": "hello"}, query="hello")
