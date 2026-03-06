"""Tests for the Castor facade API and SyscallProxy enhancements."""

import pytest

from castor import (
    AgentCheckpoint,
    CapabilityManager,
    CastorDam,
    SyscallProxy,
    castor_tool,
)
from castor.core import Castor
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


# ── Task 2: dynamic tool calls via __getattr__ ──


class TestDynamicToolCalls:
    @pytest.mark.asyncio
    async def test_proxy_dynamic_call(self, proxy):
        """proxy.search(query='hello') calls syscall('search', ...)."""
        result = await proxy.search(query="hello")
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_proxy_dynamic_multiple_args(self, proxy):
        """proxy.add(a=2, b=3) works with multiple params."""
        result = await proxy.add(a=2, b=3)
        assert result == 5

    @pytest.mark.asyncio
    async def test_proxy_dynamic_logs_syscall(self, proxy):
        """Dynamic calls go through syscall and appear in syscall_log."""
        await proxy.search(query="test")
        assert len(proxy.checkpoint.syscall_log) == 1
        record = proxy.checkpoint.syscall_log[0]
        assert record.request["tool_name"] == "search"
        assert record.request["arguments"] == {"query": "test"}

    def test_proxy_real_attrs_not_intercepted(self, proxy):
        """Real attributes like checkpoint, is_replaying are not intercepted."""
        _ = proxy.checkpoint  # should not raise
        _ = proxy.is_replaying  # should not raise

    def test_proxy_unknown_tool_raises(self, proxy):
        """Accessing a non-existent tool raises AttributeError."""
        with pytest.raises(AttributeError):
            proxy.nonexistent_tool_xyz


# ── Task 3: proxy.call(func, ...) function-reference style ──


class TestCallMethod:
    @pytest.mark.asyncio
    async def test_call_with_function_ref(self, proxy, registry):
        """proxy.call(search, query='hello') uses function's tool name."""
        search_fn = registry.get("search").func
        result = await proxy.call(search_fn, query="hello")
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_call_logs_correctly(self, proxy, registry):
        """proxy.call() logs to syscall_log with correct tool name."""
        search_fn = registry.get("search").func
        await proxy.call(search_fn, query="test")
        assert proxy.checkpoint.syscall_log[0].request["tool_name"] == "search"

    @pytest.mark.asyncio
    async def test_call_without_metadata_raises(self, proxy):
        """proxy.call() raises if function has no _castor_metadata."""

        async def plain_func(x: int) -> int:
            return x

        with pytest.raises(TypeError, match="not a @castor_tool"):
            await proxy.call(plain_func, x=1)


# ── Task 4: Castor facade class ──


class TestCastorFacade:
    def test_create_with_default_registry(self):
        """Castor() picks up tools from default_registry."""
        from castor.dam.registry import default_registry

        @castor_tool(consumes="api", cost_per_use=1.0)
        async def default_tool(x: int) -> int:
            return x * 2

        try:
            kernel = Castor()
            assert kernel._dam.registry.has_tool("default_tool")
        finally:
            default_registry._tools.pop("default_tool", None)

    def test_create_with_explicit_tools(self):
        """Castor(tools=[...]) uses only the given tools."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def explicit_tool(x: int) -> int:
            return x + 1

        kernel = Castor(tools=[explicit_tool])
        assert kernel._dam.registry.has_tool("explicit_tool")

    def test_create_with_custom_dam(self):
        """Castor(dam=...) uses the provided dam."""
        reg = ToolRegistry()
        dam = CastorDam(reg)
        kernel = Castor(dam=dam)
        assert kernel._dam is dam

    @pytest.mark.asyncio
    async def test_run_simple_agent(self):
        """kernel.run() creates checkpoint and runs agent."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def echo(msg: str) -> str:
            return f"echo: {msg}"

        kernel = Castor(tools=[echo])

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.syscall("echo", msg="hi")

        cp = await kernel.run(agent, budgets={"api": 10.0})
        assert cp.status == "COMPLETED"
        assert cp.result == "echo: hi"

    @pytest.mark.asyncio
    async def test_run_auto_generates_pid(self):
        """kernel.run() auto-generates a PID from function name."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def noop() -> str:
            return "ok"

        kernel = Castor(tools=[noop])

        async def my_agent(proxy: SyscallProxy) -> str:
            return "done"

        cp = await kernel.run(my_agent, budgets={"api": 10.0})
        assert cp.pid.startswith("my_agent-")

    @pytest.mark.asyncio
    async def test_run_with_explicit_pid(self):
        """kernel.run(pid=...) uses the given PID."""
        kernel = Castor(tools=[])

        async def agent(proxy: SyscallProxy) -> str:
            return "done"

        cp = await kernel.run(agent, pid="custom-pid")
        assert cp.pid == "custom-pid"

    @pytest.mark.asyncio
    async def test_run_without_budgets_is_unlimited(self):
        """kernel.run() without budgets allows unlimited tool calls."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def ping() -> str:
            return "pong"

        kernel = Castor(tools=[ping])

        async def agent(proxy: SyscallProxy) -> str:
            for _ in range(100):
                await proxy.syscall("ping")
            return "done"

        cp = await kernel.run(agent)
        assert cp.status == "COMPLETED"
