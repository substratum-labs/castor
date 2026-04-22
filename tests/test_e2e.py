"""End-to-end integration tests for the Castor kernel.

These tests exercise the full kernel workflow: tool registration,
agent execution, HITL suspension/resume via replay, preemption,
capability management, and checkpoint persistence.
"""

import asyncio

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.hitl import HITLHandler
from castor.scheduler.persistence import CheckpointStore
from castor.scheduler.proxy import SyscallProxy
from castor.scheduler.runner import AgentRunner

# ── Shared fixtures ──


@pytest.fixture
def registry():
    """Fresh tool registry with common tools registered."""
    reg = ToolRegistry()

    @castor_tool(consumes="network", cost_per_use=1.0, registry=reg)
    def web_search(query: str) -> list[str]:
        return [f"result for '{query}'"]

    @castor_tool(consumes="network", cost_per_use=2.0, registry=reg)
    async def fetch_url(url: str) -> str:
        return f"content of {url}"

    @castor_tool(
        consumes="disk",
        cost_per_use=1.0,
        destructive=True,
        requires_hitl=True,
        registry=reg,
    )
    def delete_files(paths: list[str]) -> int:
        return len(paths)

    @castor_tool(consumes="network", cost_per_use=0.5, registry=reg)
    def send_email(to: str, body: str) -> str:
        return f"sent to {to}"

    return reg


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def budget_mgr():
    return BudgetManager()


@pytest.fixture
def runner(gate, budget_mgr):
    return AgentRunner(gate, budget_mgr)


@pytest.fixture
def hitl():
    return HITLHandler()


@pytest.fixture
def store(tmp_path):
    return CheckpointStore(f"sqlite:///{tmp_path / 'e2e.db'}")


def make_checkpoint(budget_mgr, name="test_agent", network=100.0, disk=50.0):
    caps = budget_mgr.create_budgets({"network": network, "disk": disk})
    return AgentCheckpoint(
        pid="agent-001",
        status="RUNNING",
        agent_function_name=name,
        capabilities=caps,
    )


# ── Test 1: Happy Path ──


class TestHappyPath:
    async def test_agent_completes_with_three_syscalls(self, runner, budget_mgr):
        """Agent with 3 safe syscalls runs to completion."""

        async def research_agent(proxy: SyscallProxy) -> str:
            results = await proxy.syscall("web_search", {"query": "climate data"})
            await proxy.syscall("fetch_url", {"url": "https://data.gov/climate"})
            await proxy.syscall(
                "send_email", {"to": "boss@co.com", "body": f"Found: {results}"}
            )
            return "done"

        checkpoint = make_checkpoint(budget_mgr)
        result = await runner.run(research_agent, checkpoint)

        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 3
        assert result.syscall_log[0].request["tool_name"] == "web_search"
        assert result.syscall_log[1].request["tool_name"] == "fetch_url"
        assert result.syscall_log[2].request["tool_name"] == "send_email"
        # Budget deducted: 1.0 + 2.0 + 0.5 = 3.5
        assert result.capabilities["network"].current_usage == 3.5


# ── Test 2: HITL Suspend + Approve + Resume ──


class TestHITLApproveResume:
    async def test_suspend_approve_replay(self, gate, budget_mgr, hitl, store):
        """Agent hits destructive tool, suspends, human approves, replay completes."""

        async def cleanup_agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("web_search", {"query": "old files"})
            deleted = await proxy.syscall("delete_files", {"paths": ["/tmp/old"]})
            await proxy.syscall(
                "send_email", {"to": "admin", "body": f"Deleted {deleted} files"}
            )
            return "cleanup done"

        # First run: suspends at delete_files
        checkpoint = make_checkpoint(budget_mgr)
        runner1 = AgentRunner(gate, budget_mgr)
        await runner1.run(cleanup_agent, checkpoint)

        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert checkpoint.pending_hitl["tool_name"] == "delete_files"
        assert len(checkpoint.syscall_log) == 1  # only web_search completed

        # Persist
        store.save(checkpoint)

        # Human approves
        loaded = store.load("agent-001")
        await hitl.approve(loaded, gate, budget_mgr)
        assert loaded.status == "RUNNING"
        assert loaded.pending_hitl is None
        assert len(loaded.syscall_log) == 2  # web_search + delete_files(approved)

        # Resume via replay
        runner2 = AgentRunner(gate, budget_mgr)
        result = await runner2.run(cleanup_agent, loaded)

        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 3  # web_search + delete_files + send_email
        assert result.syscall_log[1].was_hitl is True


# ── Test 3: HITL Reject + Re-plan ──


class TestHITLReject:
    async def test_reject_triggers_replan(self, gate, budget_mgr, hitl):
        """Agent sees rejection, issues a different syscall."""

        async def adaptive_agent(proxy: SyscallProxy) -> str:
            result = await proxy.syscall("delete_files", {"paths": ["/important"]})

            # If rejected, the response is a dict with HITL_REJECTED
            if isinstance(result, dict) and result.get("status") == "HITL_REJECTED":
                # Agent re-plans: just search instead
                fallback = await proxy.syscall(
                    "web_search", {"query": "safe alternative"}
                )
                return f"re-planned: {fallback}"

            return f"deleted: {result}"

        # First run: suspends at delete_files
        checkpoint = make_checkpoint(budget_mgr)
        runner1 = AgentRunner(gate, budget_mgr)
        await runner1.run(adaptive_agent, checkpoint)

        assert checkpoint.status == "SUSPENDED_FOR_HITL"

        # Human rejects
        hitl.reject(checkpoint, "Too risky, find another way.")

        # Resume via replay
        runner2 = AgentRunner(gate, budget_mgr)
        result = await runner2.run(adaptive_agent, checkpoint)

        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 2  # delete(rejected) + web_search
        assert result.syscall_log[0].response["status"] == "HITL_REJECTED"
        assert result.syscall_log[1].request["tool_name"] == "web_search"


# ── Test 4: HITL Modify ──


class TestHITLModify:
    async def test_modify_triggers_revised_syscall(self, gate, budget_mgr, hitl):
        """Agent sees modification feedback, re-plans with revised args."""

        async def smart_agent(proxy: SyscallProxy) -> str:
            result = await proxy.syscall("delete_files", {"paths": ["/a", "/b", "/c"]})

            if isinstance(result, dict) and result.get("status") == "HITL_MODIFIED":
                feedback = result["human_feedback"]
                # Agent issues revised delete based on feedback
                revised = await proxy.syscall("delete_files", {"paths": ["/c"]})
                if isinstance(revised, dict) and revised.get("status") in (
                    "HITL_REJECTED",
                    "HITL_MODIFIED",
                ):
                    return f"still blocked: {revised}"
                return f"revised delete: {revised} (feedback: {feedback})"

            return f"deleted: {result}"

        # First run: suspends at delete_files
        checkpoint = make_checkpoint(budget_mgr)
        runner1 = AgentRunner(gate, budget_mgr)
        await runner1.run(smart_agent, checkpoint)
        assert checkpoint.status == "SUSPENDED_FOR_HITL"

        # Human modifies
        hitl.modify(checkpoint, "Only delete /c, keep /a and /b.")

        # Resume — replays the HITL_MODIFIED response, agent re-plans
        # But the revised delete_files is also destructive, so it suspends again
        runner2 = AgentRunner(gate, budget_mgr)
        await runner2.run(smart_agent, checkpoint)
        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert checkpoint.pending_hitl["arguments"]["paths"] == ["/c"]

        # Human approves the revised delete
        await hitl.approve(checkpoint, gate, budget_mgr)

        # Final resume
        runner3 = AgentRunner(gate, budget_mgr)
        result = await runner3.run(smart_agent, checkpoint)
        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 2  # original(modified) + revised(approved)


# ── Test 5: Preemption + Resume ──


class TestPreemptionResume:
    async def test_preempt_and_resume(self, gate, budget_mgr, store):
        """Agent preempted mid-execution, checkpoint persisted, resume via replay."""
        started = asyncio.Event()

        async def long_agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("web_search", {"query": "step 1"})
            started.set()
            await asyncio.sleep(10)  # simulates long work
            await proxy.syscall("web_search", {"query": "step 2"})
            return "done"

        checkpoint = make_checkpoint(budget_mgr)
        runner1 = AgentRunner(gate, budget_mgr)
        task = await runner1.run_as_task(long_agent, checkpoint)
        await started.wait()

        # Preempt
        runner1.preempt("PRIORITY_PREEMPT", {"reason": "higher priority agent"})
        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint.status == "PREEMPTED"
        assert checkpoint.preemption_reason == "PRIORITY_PREEMPT"
        assert len(checkpoint.syscall_log) == 1  # only step 1 completed

        # Persist and resume
        store.save(checkpoint)
        loaded = store.load("agent-001")
        loaded.status = "RUNNING"
        loaded.preemption_reason = None
        loaded.preemption_payload = None

        runner2 = AgentRunner(gate, budget_mgr)
        result = await runner2.run(long_agent, loaded)
        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 2


# ── Test 6: Replay Determinism ──


class TestReplayDeterminism:
    async def test_replay_produces_same_log(self, gate, budget_mgr, store):
        """Save checkpoint, replay, verify identical syscall sequence."""

        async def deterministic_agent(proxy: SyscallProxy) -> str:
            r1 = await proxy.syscall("web_search", {"query": "hello"})
            r2 = await proxy.syscall("fetch_url", {"url": "http://example.com"})
            return f"{r1} {r2}"

        # First run
        checkpoint = make_checkpoint(budget_mgr)
        runner1 = AgentRunner(gate, budget_mgr)
        await runner1.run(deterministic_agent, checkpoint)
        assert checkpoint.status == "COMPLETED"

        original_log = [(r.request, r.response) for r in checkpoint.syscall_log]

        # Replay: agent replays with cached results — no live execution
        runner2 = AgentRunner(gate, budget_mgr)
        replayed = await runner2.run(deterministic_agent, checkpoint)
        assert replayed.status == "COMPLETED"

        replayed_log = [(r.request, r.response) for r in replayed.syscall_log]

        assert original_log == replayed_log


# ── Test 7: Budget Exhaustion ──


class TestCapabilityExhaustion:
    async def test_budget_depletes_mid_run(self, gate, budget_mgr):
        """Budget runs out during agent execution.

        §2: third call raises BudgetExhaustedError. Agent catches it.
        """
        from castor.budget.manager import BudgetExhaustedError

        async def greedy_agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("web_search", {"query": "a"})
            await proxy.syscall("web_search", {"query": "b"})
            try:
                await proxy.syscall("web_search", {"query": "c"})
            except BudgetExhaustedError:
                return "budget hit after 2 calls"
            return "unexpected"

        checkpoint = make_checkpoint(budget_mgr, network=2.5, disk=50.0)
        runner = AgentRunner(gate, budget_mgr)
        result = await runner.run(greedy_agent, checkpoint)

        assert result.status == "COMPLETED"
        assert result.result == "budget hit after 2 calls"
        assert len(result.syscall_log) == 3


# ── Test 8: Validation Error → Feedback ──


class TestValidationErrorFeedback:
    async def test_bad_args_get_feedback(self, gate, budget_mgr):
        """LLM sends bad arguments, gets natural language error."""

        async def bad_agent(proxy: SyscallProxy) -> str:
            # Missing required 'query' parameter
            result = await proxy.syscall("web_search", {})
            return f"got: {result}"

        checkpoint = make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        result = await runner.run(bad_agent, checkpoint)

        assert result.status == "COMPLETED"
        assert len(result.syscall_log) == 1
        response = result.syscall_log[0].response
        assert response["status"] == "VALIDATION_ERROR"
        assert "query" in response["feedback_message"]
        assert "fix" in response["feedback_message"].lower()
