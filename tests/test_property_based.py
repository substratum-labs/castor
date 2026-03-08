"""Property-based tests for Castor invariants using Hypothesis."""

from hypothesis import given, settings
from hypothesis import strategies as st

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.stream.proxy import SyscallProxy

# ── Strategies ──

budget_amount = st.floats(
    min_value=1.0,
    max_value=1000.0,
    allow_nan=False,
    allow_infinity=False,
)
cost_amount = st.floats(
    min_value=0.1,
    max_value=10.0,
    allow_nan=False,
    allow_infinity=False,
)


# ── Property 1: Budget Conservation ──


class TestBudgetConservation:
    @given(initial=budget_amount, cost=cost_amount)
    def test_deduct_refund_identity(self, initial, cost):
        """deduct then refund returns to original usage."""
        if cost > initial:
            return  # skip impossible cases
        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"res": initial})
        cap_mgr.deduct(caps, "res", cost)
        cap_mgr.refund(caps, "res", cost)
        assert abs(caps["res"].current_usage) < 1e-9

    @given(
        parent_budget=budget_amount,
        child_budget=st.floats(
            min_value=1.0,
            max_value=100.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        child_usage=st.floats(
            min_value=0.0,
            max_value=50.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_delegate_reclaim_conservation(
        self,
        parent_budget,
        child_budget,
        child_usage,
    ):
        """delegate + child usage + reclaim = parent deducted by child_usage only."""
        if child_budget > parent_budget or child_usage > child_budget:
            return
        cap_mgr = CapabilityManager()
        parent_caps = cap_mgr.create_capabilities({"res": parent_budget})
        child_caps = cap_mgr.delegate(parent_caps, {"res": child_budget})
        child_caps["res"].current_usage = child_usage
        cap_mgr.reclaim(parent_caps, child_caps)
        # Parent should have lost exactly child_usage
        assert abs(parent_caps["res"].current_usage - child_usage) < 1e-9

    @given(
        budget=budget_amount,
        costs=st.lists(cost_amount, min_size=1, max_size=20),
    )
    def test_sequential_deduct_totals(self, budget, costs):
        """Sum of deductions equals total current_usage."""
        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"res": budget})
        total_deducted = 0.0
        for cost in costs:
            if total_deducted + cost > budget:
                break
            cap_mgr.deduct(caps, "res", cost)
            total_deducted += cost
        assert abs(caps["res"].current_usage - total_deducted) < 1e-6


# ── Property 2: Replay Identity ──


class TestReplayIdentity:
    @given(num_syscalls=st.integers(min_value=1, max_value=20))
    @settings(max_examples=50)
    async def test_replay_produces_identical_results(self, num_syscalls):
        """Replaying N syscalls from cache returns identical values."""
        registry = ToolRegistry()

        @castor_tool(consumes="test", cost_per_use=0.0, registry=registry)
        def echo(value: str) -> str:
            return f"echo:{value}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()

        # Build a syscall log
        log = []
        for i in range(num_syscalls):
            log.append(
                SyscallRecord(
                    request={"tool_name": "echo", "arguments": {"value": f"msg-{i}"}},
                    response=f"echo:msg-{i}",
                )
            )

        # Replay from the log
        caps = cap_mgr.create_capabilities({"test": 1000.0})
        cp = AgentCheckpoint(
            pid="prop-test",
            status="RUNNING",
            agent_function_name="test",
            capabilities=caps,
            syscall_log=log,
        )
        proxy = SyscallProxy(cp, gate, cap_mgr)

        for i in range(num_syscalls):
            result = await proxy.syscall("echo", {"value": f"msg-{i}"})
            assert result == f"echo:msg-{i}"

        assert not proxy.is_replaying


# ── Property 3: HITL Modify Preserves Original ──


class TestHITLModifyInvariant:
    @given(
        paths=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=5),
        feedback=st.text(min_size=1, max_size=100),
    )
    def test_modify_never_mutates_original_request(self, paths, feedback):
        """HITL modify logs original request unmodified."""
        from castor.stream.hitl import HITLHandler

        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"test": 100.0})
        cp = AgentCheckpoint(
            pid="hitl-test",
            status="SUSPENDED_FOR_HITL",
            agent_function_name="test",
            capabilities=caps,
            pending_hitl={
                "tool_name": "delete_files",
                "arguments": {"paths": paths},
            },
        )
        original_request = {
            "tool_name": "delete_files",
            "arguments": {"paths": list(paths)},
        }

        handler = HITLHandler()
        handler.modify(cp, feedback)

        logged = cp.syscall_log[-1]
        assert logged.request == original_request
        assert logged.response["status"] == "HITL_MODIFIED"
        assert logged.response["human_feedback"] == feedback
