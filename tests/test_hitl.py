"""Tests for HITL Feedback Handler."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.hitl import HITLHandler


@pytest.fixture
def registry():
    reg = ToolRegistry()

    @castor_tool(
        consumes="disk",
        cost_per_use=1.0,
        destructive=True,
        requires_hitl=True,
        registry=reg,
    )
    def delete_files(paths: list[str]) -> int:
        return len(paths)

    return reg


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


@pytest.fixture
def handler():
    return HITLHandler()


def make_suspended_checkpoint(cap_mgr):
    caps = cap_mgr.create_capabilities({"disk": 50.0})
    return AgentCheckpoint(
        pid="test-001",
        status="SUSPENDED_FOR_HITL",
        agent_function_name="test_agent",
        capabilities=caps,
        pending_hitl={
            "tool_name": "delete_files",
            "arguments": {"paths": ["/tmp/a", "/tmp/b"]},
        },
    )


class TestApprove:
    async def test_approve_executes_and_logs(self, handler, gate, cap_mgr):
        checkpoint = make_suspended_checkpoint(cap_mgr)
        await handler.approve(checkpoint, gate, cap_mgr)

        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None
        assert len(checkpoint.syscall_log) == 1

        record = checkpoint.syscall_log[0]
        assert record.was_hitl is True
        assert record.request["tool_name"] == "delete_files"
        assert record.response == 2  # len(["/tmp/a", "/tmp/b"])

    async def test_approve_deducts_capability(self, handler, gate, cap_mgr):
        checkpoint = make_suspended_checkpoint(cap_mgr)
        await handler.approve(checkpoint, gate, cap_mgr)
        assert checkpoint.capabilities["disk"].current_usage == 1.0

    async def test_approve_no_pending_raises(self, handler, gate, cap_mgr):
        caps = cap_mgr.create_capabilities({"disk": 50.0})
        checkpoint = AgentCheckpoint(
            pid="test-001",
            status="RUNNING",
            agent_function_name="test_agent",
            capabilities=caps,
        )
        with pytest.raises(ValueError, match="No pending"):
            await handler.approve(checkpoint, gate, cap_mgr)


class TestReject:
    def test_reject_logs_feedback(self, handler, cap_mgr):
        checkpoint = make_suspended_checkpoint(cap_mgr)
        handler.reject(checkpoint, "Too risky, do not delete.")

        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None
        assert len(checkpoint.syscall_log) == 1

        record = checkpoint.syscall_log[0]
        assert record.was_hitl is True
        assert record.response["status"] == "HITL_REJECTED"
        assert record.response["human_feedback"] == "Too risky, do not delete."

    def test_reject_no_pending_raises(self, handler, cap_mgr):
        caps = cap_mgr.create_capabilities({"disk": 50.0})
        checkpoint = AgentCheckpoint(
            pid="test-001",
            status="RUNNING",
            agent_function_name="test_agent",
            capabilities=caps,
        )
        with pytest.raises(ValueError, match="No pending"):
            handler.reject(checkpoint, "no")


class TestModify:
    def test_modify_logs_feedback(self, handler, cap_mgr):
        checkpoint = make_suspended_checkpoint(cap_mgr)
        handler.modify(checkpoint, "Only delete files older than 7 days.")

        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None
        assert len(checkpoint.syscall_log) == 1

        record = checkpoint.syscall_log[0]
        assert record.was_hitl is True
        assert record.response["status"] == "HITL_MODIFIED"
        assert "older than 7 days" in record.response["human_feedback"]

    def test_modify_preserves_original_request(self, handler, cap_mgr):
        checkpoint = make_suspended_checkpoint(cap_mgr)
        handler.modify(checkpoint, "Keep recent files.")

        record = checkpoint.syscall_log[0]
        # Original request is preserved — not mutated
        assert record.request == {
            "tool_name": "delete_files",
            "arguments": {"paths": ["/tmp/a", "/tmp/b"]},
        }

    def test_modify_no_pending_raises(self, handler, cap_mgr):
        caps = cap_mgr.create_capabilities({"disk": 50.0})
        checkpoint = AgentCheckpoint(
            pid="test-001",
            status="RUNNING",
            agent_function_name="test_agent",
            capabilities=caps,
        )
        with pytest.raises(ValueError, match="No pending"):
            handler.modify(checkpoint, "change it")
