"""Tests for integration API improvements (A1–A3, B1–B2, B4)."""

from __future__ import annotations

import pytest

from castor import (
    AgentCheckpoint,
    Capability,
    Castor,
    CheckpointStoreProtocol,
    MemoryCheckpointStore,
    SyscallGate,
    SyscallProxy,
    ToolMetadata,
    castor_tool,
)
from castor.capability.manager import CapabilityExhaustedError, CapabilityManager
from castor.gate.registry import ToolRegistry
from castor.scheduler.persistence import CheckpointNotFoundError

# ── A1: Budget skip on missing resource ────────────────────────────────────


class TestBudgetSkipMissingResource:
    """deduct() should no-op when resource type is not tracked."""

    def test_deduct_missing_resource_noop(self):
        mgr = CapabilityManager()
        caps: dict[str, Capability] = {}  # No budgets configured
        # Should NOT raise — missing resource = not tracked
        mgr.deduct(caps, "api", 1.0)

    def test_deduct_tracked_resource_still_enforced(self):
        mgr = CapabilityManager()
        caps = mgr.create_capabilities({"api": 5.0})
        mgr.deduct(caps, "api", 3.0)
        assert caps["api"].current_usage == 3.0
        # Should still raise when budget is actually exhausted
        with pytest.raises(CapabilityExhaustedError):
            mgr.deduct(caps, "api", 3.0)

    def test_deduct_untracked_alongside_tracked(self):
        mgr = CapabilityManager()
        caps = mgr.create_capabilities({"api": 10.0})
        # "storage" is not in caps — should no-op
        mgr.deduct(caps, "storage", 5.0)
        # "api" should still work normally
        mgr.deduct(caps, "api", 2.0)
        assert caps["api"].current_usage == 2.0

    def test_check_missing_resource_returns_true(self):
        mgr = CapabilityManager()
        caps: dict[str, Capability] = {}
        # Not tracked → allowed
        assert mgr.check(caps, "api", 100.0) is True

    def test_check_tracked_resource_still_enforced(self):
        mgr = CapabilityManager()
        caps = mgr.create_capabilities({"api": 5.0})
        assert mgr.check(caps, "api", 3.0) is True
        assert mgr.check(caps, "api", 6.0) is False


# ── A1 integration: no-budget agent runs without errors ────────────────────


@castor_tool(consumes="api", cost_per_use=1.0)
def _cost_tool(query: str) -> str:
    return f"result: {query}"


@pytest.mark.asyncio
async def test_agent_runs_without_budgets():
    """An agent with cost-bearing tools should work when no budgets are set."""
    kernel = Castor(tools=[_cost_tool])

    async def agent(proxy: SyscallProxy):
        return await proxy.syscall("_cost_tool", {"query": "hello"})

    cp = await kernel.run(agent)  # budgets=None → no enforcement
    assert cp.status == "COMPLETED"
    assert cp.result == "result: hello"


# ── A2: ToolMetadata.from_function() ───────────────────────────────────────


class TestToolMetadataFromFunction:
    def test_from_sync_function(self):
        def search(query: str, limit: int = 10) -> str:
            return f"{query}:{limit}"

        meta = ToolMetadata.from_function(search)
        assert meta.tool_name == "search"
        assert meta.is_async is False
        assert meta.func is search
        assert meta.consumes == "_default"
        assert meta.cost_per_use == 0.0
        # Schema should have "query" as required and "limit" as optional
        assert "query" in str(meta.input_schema)

    def test_from_async_function(self):
        async def fetch_data(url: str) -> dict:
            return {"url": url}

        meta = ToolMetadata.from_function(fetch_data)
        assert meta.tool_name == "fetch_data"
        assert meta.is_async is True

    def test_from_function_with_overrides(self):
        def delete_item(item_id: str) -> str:
            return f"deleted {item_id}"

        meta = ToolMetadata.from_function(
            delete_item,
            consumes="api",
            cost_per_use=2.0,
            destructive=True,
            requires_hitl=True,
            timeout_seconds=30.0,
        )
        assert meta.consumes == "api"
        assert meta.cost_per_use == 2.0
        assert meta.destructive is True
        assert meta.requires_hitl is True
        assert meta.timeout_seconds == 30.0

    def test_from_function_registers_in_registry(self):
        def my_tool(x: int) -> int:
            return x * 2

        meta = ToolMetadata.from_function(my_tool)
        registry = ToolRegistry()
        registry.register(meta)
        assert registry.has_tool("my_tool")
        assert registry.get("my_tool").func is my_tool


# ── A3: MemoryCheckpointStore ──────────────────────────────────────────────


class TestMemoryCheckpointStore:
    def test_save_and_load(self):
        store = MemoryCheckpointStore()
        cp = AgentCheckpoint(
            pid="test-1",
            status="COMPLETED",
            agent_function_name="test_agent",
            capabilities={},
            result="done",
        )
        store.save(cp)
        loaded = store.load("test-1")
        assert loaded.pid == "test-1"
        assert loaded.result == "done"

    def test_load_not_found(self):
        store = MemoryCheckpointStore()
        with pytest.raises(CheckpointNotFoundError):
            store.load("nonexistent")

    def test_delete(self):
        store = MemoryCheckpointStore()
        cp = AgentCheckpoint(
            pid="test-1",
            status="COMPLETED",
            agent_function_name="test_agent",
            capabilities={},
        )
        store.save(cp)
        store.delete("test-1")
        with pytest.raises(CheckpointNotFoundError):
            store.load("test-1")

    def test_delete_nonexistent_noop(self):
        store = MemoryCheckpointStore()
        store.delete("nonexistent")  # Should not raise

    def test_list_pids(self):
        store = MemoryCheckpointStore()
        for i in range(3):
            cp = AgentCheckpoint(
                pid=f"test-{i}",
                status="COMPLETED",
                agent_function_name="agent",
                capabilities={},
            )
            store.save(cp)
        pids = store.list_pids()
        assert set(pids) == {"test-0", "test-1", "test-2"}

    def test_overwrite(self):
        store = MemoryCheckpointStore()
        cp1 = AgentCheckpoint(
            pid="test-1",
            status="RUNNING",
            agent_function_name="agent",
            capabilities={},
        )
        store.save(cp1)
        cp2 = AgentCheckpoint(
            pid="test-1",
            status="COMPLETED",
            agent_function_name="agent",
            capabilities={},
            result="finished",
        )
        store.save(cp2)
        loaded = store.load("test-1")
        assert loaded.status == "COMPLETED"
        assert loaded.result == "finished"

    def test_satisfies_protocol(self):
        store = MemoryCheckpointStore()
        assert isinstance(store, CheckpointStoreProtocol)


# ── A3: CheckpointStoreProtocol ────────────────────────────────────────────


def test_sqlite_store_satisfies_protocol():
    """Verify the existing SQLite CheckpointStore satisfies the protocol."""
    from castor.scheduler.persistence import CheckpointStore

    store = CheckpointStore("sqlite:///:memory:")
    assert isinstance(store, CheckpointStoreProtocol)


# ── B1: Public properties on Castor ────────────────────────────────────────


class TestCastorPublicProperties:
    def test_gate_property(self):
        kernel = Castor(tools=[_cost_tool])
        assert isinstance(kernel.gate, SyscallGate)
        assert kernel.gate.registry.has_tool("_cost_tool")

    def test_capability_manager_property(self):
        kernel = Castor(tools=[_cost_tool])
        assert isinstance(kernel.capability_manager, CapabilityManager)

    def test_store_property_none(self):
        kernel = Castor(tools=[_cost_tool])
        assert kernel.store is None

    def test_store_property_with_memory_store(self):
        store = MemoryCheckpointStore()
        kernel = Castor(tools=[_cost_tool], store=store)
        assert kernel.store is store


# ── B2: Default budgets ───────────────────────────────────────────────────


class TestDefaultBudgets:
    @pytest.mark.asyncio
    async def test_default_budgets_used_when_none(self):
        kernel = Castor(tools=[_cost_tool], default_budgets={"api": 100.0})

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall("_cost_tool", {"query": "test"})

        # budgets=None → falls back to default_budgets
        cp = await kernel.run(agent)
        assert cp.status == "COMPLETED"
        assert cp.capabilities["api"].max_budget == 100.0
        assert cp.capabilities["api"].current_usage == 1.0

    @pytest.mark.asyncio
    async def test_explicit_budgets_override_default(self):
        kernel = Castor(tools=[_cost_tool], default_budgets={"api": 100.0})

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall("_cost_tool", {"query": "test"})

        # Explicit budgets override the default
        cp = await kernel.run(agent, budgets={"api": 50.0})
        assert cp.capabilities["api"].max_budget == 50.0

    @pytest.mark.asyncio
    async def test_no_default_no_explicit_means_unlimited(self):
        kernel = Castor(tools=[_cost_tool])

        async def agent(proxy: SyscallProxy):
            # Call cost tool many times — should never fail
            for _ in range(20):
                await proxy.syscall("_cost_tool", {"query": "test"})
            return "done"

        cp = await kernel.run(agent)  # No budgets at all
        assert cp.status == "COMPLETED"
        assert cp.capabilities == {}


# ── Auto-budget inference ─────────────────────────────────────────────────


class TestAutoBudget:
    @pytest.mark.asyncio
    async def test_auto_budget_infers_from_tools(self):
        kernel = Castor(tools=[_cost_tool], auto_budget=50)

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall("_cost_tool", {"query": "test"})

        cp = await kernel.run(agent)
        assert cp.status == "COMPLETED"
        assert cp.capabilities["api"].max_budget == 50
        assert cp.capabilities["api"].current_usage == 1.0

    @pytest.mark.asyncio
    async def test_auto_budget_explicit_overrides(self):
        kernel = Castor(tools=[_cost_tool], auto_budget=50)

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall("_cost_tool", {"query": "test"})

        cp = await kernel.run(agent, budgets={"api": 10})
        assert cp.capabilities["api"].max_budget == 10

    @pytest.mark.asyncio
    async def test_auto_budget_default_budgets_overrides(self):
        kernel = Castor(tools=[_cost_tool], default_budgets={"api": 25}, auto_budget=50)

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall("_cost_tool", {"query": "test"})

        cp = await kernel.run(agent)
        assert cp.capabilities["api"].max_budget == 25  # default_budgets wins

    @pytest.mark.asyncio
    async def test_auto_budget_skips_zero_cost_tools(self):
        @castor_tool(consumes="free", cost_per_use=0)
        def _free_tool(x: str) -> str:
            return x

        kernel = Castor(tools=[_free_tool], auto_budget=100)

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall("_free_tool", {"x": "test"})

        cp = await kernel.run(agent)
        assert cp.capabilities == {}  # no budget created for zero-cost tools

    @pytest.mark.asyncio
    async def test_auto_budget_destructive_only_no_budget(self):
        @castor_tool(destructive=True)
        def _destructive_tool(path: str) -> str:
            return f"deleted {path}"

        kernel = Castor(tools=[_destructive_tool], auto_budget=100)

        async def agent(proxy: SyscallProxy):
            return await proxy.syscall("_destructive_tool", {"path": "/tmp/x"})

        cp = await kernel.run(agent)
        # destructive with cost=0 → HITL but no budget
        assert cp.status == "SUSPENDED_FOR_HITL"
        assert cp.capabilities == {}


# ── B4: Empty schema validation passthrough ───────────────────────────────


class TestEmptySchemaPassthrough:
    def test_validate_empty_schema_passthrough(self):
        registry = ToolRegistry()
        meta = ToolMetadata(
            tool_name="llm_inference",
            input_schema={},  # Empty → skip validation
            func=lambda **kwargs: kwargs,
            is_async=False,
        )
        registry.register(meta)
        gate = SyscallGate(registry)

        # Complex args should pass through without validation
        args = {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "search"}],
            "nested": {"deep": [1, 2, 3]},
        }
        result = gate.validate("llm_inference", args)
        assert result == args

    def test_validate_with_schema_still_validates(self):
        @castor_tool()
        def typed_tool(name: str, count: int = 5) -> str:
            return f"{name}:{count}"

        registry = ToolRegistry()
        meta = getattr(typed_tool, "_castor_metadata")
        registry.register(meta)
        gate = SyscallGate(registry)

        # Valid args
        result = gate.validate("typed_tool", {"name": "test"})
        assert result["name"] == "test"
        assert result["count"] == 5

        # Invalid args still raise
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            gate.validate("typed_tool", {"name": 123, "count": "not_an_int"})
