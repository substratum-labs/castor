"""Tests for Protocol interface conformance and substitutability."""

from __future__ import annotations

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.registry import ToolMetadata, ToolRegistry
from castor.gate.validator import SyscallGate
from castor.mmu.cold_storage import InMemoryColdStorage
from castor.mmu.core import MMU
from castor.protocols import (
    AgentRegistryProtocol,
    BudgetProtocol,
    CheckpointStoreProtocol,
    GateProtocol,
)
from castor.scheduler.agent_registry import AgentRegistry
from castor.scheduler.persistence import (
    CheckpointStoreProtocol as ReExportedProtocol,
)
from castor.scheduler.persistence import (
    MemoryCheckpointStore,
)

# ---------------------------------------------------------------------------
# Structural conformance: concrete classes satisfy Protocols
# ---------------------------------------------------------------------------


class TestGateProtocolConformance:
    def test_syscall_gate_is_gate_protocol(self):
        registry = ToolRegistry()
        gate = SyscallGate(registry)
        assert isinstance(gate, GateProtocol)

    def test_gate_has_tool_delegation(self):
        registry = ToolRegistry()
        meta = ToolMetadata(
            tool_name="ping",
            func=lambda: "pong",
            input_schema={},
        )
        registry.register(meta)
        gate = SyscallGate(registry)
        assert gate.has_tool("ping")
        assert not gate.has_tool("nonexistent")

    def test_gate_list_tools_delegation(self):
        registry = ToolRegistry()
        meta = ToolMetadata(
            tool_name="ping",
            func=lambda: "pong",
            input_schema={},
        )
        registry.register(meta)
        gate = SyscallGate(registry)
        assert "ping" in gate.list_tools()


class TestBudgetProtocolConformance:
    def test_capability_manager_is_budget_protocol(self):
        assert isinstance(CapabilityManager(), BudgetProtocol)


class TestCheckpointStoreProtocolConformance:
    def test_memory_store_is_checkpoint_store_protocol(self):
        assert isinstance(MemoryCheckpointStore(), CheckpointStoreProtocol)

    def test_re_export_is_same_protocol(self):
        """Backward compat: importing from persistence.py gives the same Protocol."""
        assert ReExportedProtocol is CheckpointStoreProtocol


class TestMMUProtocolConformance:
    def test_mmu_is_mmu_protocol(self):
        registry = ToolRegistry()
        cold = InMemoryColdStorage()
        mmu = MMU(registry, cold_storage=cold, hard_watermark=100)
        # runtime_checkable doesn't verify properties, so check manually
        assert hasattr(mmu, "kernel_tool_names")
        assert hasattr(mmu, "check_and_evict")


class TestAgentRegistryProtocolConformance:
    def test_agent_registry_is_protocol(self):
        assert isinstance(AgentRegistry(), AgentRegistryProtocol)


# ---------------------------------------------------------------------------
# Mock substitution: Protocol-only implementations work with SyscallProxy
# ---------------------------------------------------------------------------


class TestMockSubstitution:
    @pytest.mark.asyncio
    async def test_proxy_accepts_protocol_implementations(self):
        """SyscallProxy works with any GateProtocol/BudgetProtocol implementation."""
        from castor.models.checkpoint import AgentCheckpoint
        from castor.scheduler.proxy import SyscallProxy

        # Minimal mock Gate that satisfies GateProtocol
        class MockGate:
            def validate(self, tool_name, arguments):
                return arguments or {}

            def get_tool_meta(self, tool_name):
                return ToolMetadata(
                    tool_name=tool_name,
                    func=lambda: "mock_result",
                    input_schema={},
                    cost_per_use=0.0,
                )

            async def execute(self, tool_name, validated_args):
                return "mock_result"

            def format_validation_error(self, tool_name, error):
                from castor.models.capability import SyscallResponse

                return SyscallResponse(status="ERROR", feedback_message="err")

            def has_tool(self, tool_name):
                return tool_name == "mock_tool"

            def list_tools(self):
                return ["mock_tool"]

        # Minimal mock Budget
        class MockBudget:
            def create_capabilities(self, specs):
                return {}

            def check(self, capabilities, resource_type, cost):
                return True

            def deduct(self, capabilities, resource_type, cost):
                pass

            def refund(self, capabilities, resource_type, cost):
                pass

            def delegate(self, parent_caps, requested):
                return {}

            def reclaim(self, parent_caps, child_caps):
                pass

        gate = MockGate()
        budget = MockBudget()
        cp = AgentCheckpoint(
            pid="test-mock",
            status="RUNNING",
            agent_function_name="test",
            capabilities={},
        )

        proxy = SyscallProxy(cp, gate, budget)
        result = await proxy.syscall("mock_tool")
        assert result == "mock_result"
