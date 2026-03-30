"""Tests for Speculative Execution Mode."""

from __future__ import annotations

import pytest

from castor import AgentCheckpoint, Castor, ExecutionSummary
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry


@castor_tool(
    consumes="api", cost_per_use=0.0, destructive=True, registry=ToolRegistry()
)
async def dangerous_tool(x: int) -> str:
    return f"destroyed {x}"


@castor_tool(consumes="api", cost_per_use=1.0, registry=ToolRegistry())
async def safe_tool(x: int) -> int:
    return x * 2


# Shared registry for tests
_registry = ToolRegistry()
_registry.register(dangerous_tool._castor_metadata)
_registry.register(safe_tool._castor_metadata)


class TestSpeculativeMode:
    @pytest.mark.asyncio
    async def test_speculative_skips_hitl(self):
        """In speculative mode, destructive tools don't suspend."""
        from castor.gate.validator import SyscallGate

        kernel = Castor(gate=SyscallGate(_registry))

        async def agent(proxy):
            r1 = await proxy.syscall("safe_tool", {"x": 5})
            r2 = await proxy.syscall("dangerous_tool", {"x": 42})
            return {"safe": r1, "dangerous": r2}

        cp = await kernel.run(agent, budgets={"api": 100.0}, speculative=True)

        # Should complete without suspending
        assert cp.status == "COMPLETED"
        assert cp.result["safe"] == 10
        assert cp.result["dangerous"] == "destroyed 42"

    @pytest.mark.asyncio
    async def test_non_speculative_suspends_on_destructive(self):
        """Without speculative mode, destructive tools suspend for HITL."""
        from castor.gate.validator import SyscallGate

        kernel = Castor(gate=SyscallGate(_registry))

        async def agent(proxy):
            await proxy.syscall("dangerous_tool", {"x": 42})
            return "done"

        cp = await kernel.run(agent, budgets={"api": 100.0}, speculative=False)

        assert cp.status == "SUSPENDED_FOR_HITL"


class TestExecutionSummary:
    @pytest.mark.asyncio
    async def test_scan_flags_destructive_tools(self):
        """scan() flags destructive tool calls."""
        from castor.gate.validator import SyscallGate

        kernel = Castor(gate=SyscallGate(_registry))

        async def agent(proxy):
            await proxy.syscall("safe_tool", {"x": 1})
            await proxy.syscall("safe_tool", {"x": 2})
            await proxy.syscall("dangerous_tool", {"x": 3})
            return "done"

        cp = await kernel.run(agent, budgets={"api": 100.0}, speculative=True)
        summary = kernel.scan(cp)

        assert isinstance(summary, ExecutionSummary)
        assert summary.total_steps == 3
        assert summary.flagged_count == 1
        assert summary.auto_verified == 2
        assert summary.flagged[0].tool_name == "dangerous_tool"
        assert "destructive" in summary.flagged[0].reason
        assert summary.tools_used == {"safe_tool": 2, "dangerous_tool": 1}

    @pytest.mark.asyncio
    async def test_scan_no_flags_for_safe_tools(self):
        """scan() with only safe tools → no flags."""
        from castor.gate.validator import SyscallGate

        kernel = Castor(gate=SyscallGate(_registry))

        async def agent(proxy):
            await proxy.syscall("safe_tool", {"x": 1})
            await proxy.syscall("safe_tool", {"x": 2})
            return "done"

        cp = await kernel.run(agent, budgets={"api": 100.0}, speculative=True)
        summary = kernel.scan(cp)

        assert summary.total_steps == 2
        assert summary.flagged_count == 0
        assert summary.auto_verified == 2


class TestNeedsReviewFlag:
    @pytest.mark.asyncio
    async def test_destructive_tool_flagged_at_execution_time(self):
        """In speculative mode, destructive records carry needs_review=True."""
        from castor.gate.validator import SyscallGate

        kernel = Castor(gate=SyscallGate(_registry))

        async def agent(proxy):
            await proxy.syscall("safe_tool", {"x": 1})
            await proxy.syscall("dangerous_tool", {"x": 2})
            return "done"

        cp = await kernel.run(agent, budgets={"api": 100.0}, speculative=True)

        # safe_tool record: not flagged
        assert cp.syscall_log[0].needs_review is False
        assert cp.syscall_log[0].review_reason is None

        # dangerous_tool record: flagged at execution time
        assert cp.syscall_log[1].needs_review is True
        assert "destructive" in cp.syscall_log[1].review_reason

    @pytest.mark.asyncio
    async def test_non_speculative_no_flags(self):
        """Non-speculative: no needs_review flag (HITL suspends instead)."""
        from castor.gate.validator import SyscallGate

        kernel = Castor(gate=SyscallGate(_registry))

        async def agent(proxy):
            await proxy.syscall("safe_tool", {"x": 1})
            # dangerous_tool would suspend, so agent never reaches it
            return "done"

        cp = await kernel.run(agent, budgets={"api": 100.0}, speculative=False)

        assert cp.status == "COMPLETED"
        assert len(cp.syscall_log) == 1
        assert cp.syscall_log[0].needs_review is False


class TestCheckpointFork:
    def test_fork_keeps_steps_before(self):
        from castor.models.checkpoint import SyscallRecord

        cp = AgentCheckpoint(
            pid="test-1",
            status="COMPLETED",
            agent_function_name="agent",
            capabilities={},
            syscall_log=[
                SyscallRecord(request={"tool_name": f"step{i}"}, response=f"r{i}")
                for i in range(5)
            ],
            result="done",
        )

        forked = cp.fork(at_step=3)

        assert len(forked.syscall_log) == 3
        assert forked.syscall_log[0].request["tool_name"] == "step0"
        assert forked.syscall_log[2].request["tool_name"] == "step2"
        assert forked.status == "RUNNING"
        assert forked.result is None
        assert forked.pid == "test-1::fork-3"

    def test_fork_is_deep_copy(self):
        from castor.models.checkpoint import SyscallRecord

        cp = AgentCheckpoint(
            pid="test-1",
            status="COMPLETED",
            agent_function_name="agent",
            capabilities={},
            syscall_log=[
                SyscallRecord(request={"tool_name": "a"}, response="r")
            ],
            result="done",
        )

        forked = cp.fork(at_step=1)
        forked.syscall_log[0].response = "modified"

        # Original not affected
        assert cp.syscall_log[0].response == "r"

    def test_fork_at_zero(self):
        cp = AgentCheckpoint(
            pid="test-1",
            status="COMPLETED",
            agent_function_name="agent",
            capabilities={},
            syscall_log=[],
            result="done",
        )

        forked = cp.fork(at_step=0)
        assert len(forked.syscall_log) == 0
        assert forked.status == "RUNNING"

    def test_fork_invalid_step_raises(self):
        import pytest

        cp = AgentCheckpoint(
            pid="test-1",
            status="COMPLETED",
            agent_function_name="agent",
            capabilities={},
            syscall_log=[],
        )

        with pytest.raises(ValueError, match="out of range"):
            cp.fork(at_step=5)

        with pytest.raises(ValueError, match="out of range"):
            cp.fork(at_step=-1)

    def test_fork_clears_preemption_fields(self):
        cp = AgentCheckpoint(
            pid="test-1",
            status="PREEMPTED",
            agent_function_name="agent",
            capabilities={},
            preemption_reason="timeout",
            preemption_payload={"x": 1},
            partial_work="partial",
            pending_hitl={"tool_name": "t"},
        )

        forked = cp.fork(at_step=0)
        assert forked.preemption_reason is None
        assert forked.preemption_payload is None
        assert forked.partial_work is None
        assert forked.pending_hitl is None
