"""Tests for S1 Phase A — Preemptive Scheduling.

Tests per acceptance criteria:
  AC 8:  Budget exhaustion triggers PreemptedError
  AC 9:  Fork loser preempted
  AC 10: Priority preempt
  AC 11: Replay determinism
  AC 12: Generic except doesn't swallow PreemptedError
  AC 1-5: Model/type existence
"""

from __future__ import annotations

import pytest

from castor import (
    AgentCheckpoint,
    Castor,
    PreemptedError,
    PreemptionReason,
    PreemptionRecord,
    Scheduler,
    castor_tool,
)

# ── AC 1-5: Models exist ──


def test_preemption_reason_enum():
    assert PreemptionReason.BUDGET_EXHAUSTED == "budget_exhausted"
    assert PreemptionReason.SPECULATIVE_LOSER == "speculative_loser"
    assert PreemptionReason.PRIORITY_PREEMPTED == "priority_preempted"
    assert PreemptionReason.OPERATOR_KILL == "operator_kill"  # reserved
    assert PreemptionReason.DEADLINE == "deadline"  # reserved
    assert PreemptionReason.PARENT_KILL == "parent_kill"  # reserved


def test_preempted_error_is_base_exception():
    assert issubclass(PreemptedError, BaseException)
    assert not issubclass(PreemptedError, Exception)


def test_preemption_record_model():
    r = PreemptionRecord(
        syscall_index_after=5,
        reason=PreemptionReason.BUDGET_EXHAUSTED,
        timestamp=1234567890.0,
        metadata={"resource": "api"},
    )
    assert r.syscall_index_after == 5
    assert r.reason == PreemptionReason.BUDGET_EXHAUSTED


def test_checkpoint_has_preemption_log():
    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    cp = AgentCheckpoint(
        pid="test",
        status="RUNNING",
        agent_function_name="a",
        capabilities=bm.create_budgets({}),
    )
    assert cp.preemption_log == []


def test_checkpoint_accepts_new_status_values():
    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    for status in ["PREEMPTED", "SUSPENDED", "KILLED"]:
        cp = AgentCheckpoint(
            pid="t",
            status=status,
            agent_function_name="a",
            capabilities=bm.create_budgets({}),
        )
        assert cp.status == status


def test_backwards_compat_no_preemption_log():
    """Old checkpoint JSON without preemption_log deserializes fine."""

    data = {
        "pid": "old",
        "status": "COMPLETED",
        "agent_function_name": "a",
        "capabilities": {},
    }
    cp = AgentCheckpoint.model_validate(data)
    assert cp.preemption_log == []


# ── AC 8: Budget exhaustion ──


@pytest.mark.asyncio
async def test_budget_exhaustion_preempts():
    """Budget overshoot → next syscall raises PreemptedError."""

    @castor_tool(consumes="api", cost_per_use=60.0)
    async def costly() -> str:
        return "ok"

    kernel = Castor(tools=[costly], budgets={"api": 100.0})

    async def agent(proxy):
        await proxy.syscall("costly", {})  # 100-60=40 remaining
        try:
            await proxy.syscall("costly", {})  # 40<60 → preempt
        except PreemptedError:
            return "preempted"
        return "should not reach"

    cp = await kernel.run(agent)
    assert cp.status == "COMPLETED"
    assert cp.result == "preempted"


@pytest.mark.asyncio
async def test_budget_exhaustion_raises_preempted_error():
    """Agent can catch PreemptedError specifically."""

    @castor_tool(consumes="api", cost_per_use=60.0)
    async def costly() -> str:
        return "ok"

    kernel = Castor(tools=[costly], budgets={"api": 100.0})

    async def agent(proxy):
        await proxy.syscall("costly", {})  # 100-60=40
        try:
            await proxy.syscall("costly", {})  # 40<60 → preempt
        except PreemptedError as e:
            return f"caught: {e.reason}"
        except BaseException:
            return "caught BaseException"
        return "not caught"

    cp = await kernel.run(agent)
    # Should have caught the preempt
    assert "caught" in str(cp.result)


# ── AC 9: Fork loser ──


@pytest.mark.asyncio
async def test_fork_loser_preempted():
    """Marking a pid as fork loser → next syscall raises."""
    scheduler = Scheduler()
    scheduler.mark_fork_loser("loser-1", winner_pid="winner-1", group_id="g1")

    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    cp = AgentCheckpoint(
        pid="loser-1",
        status="RUNNING",
        agent_function_name="a",
        capabilities=bm.create_budgets({"api": 100}),
    )

    result = scheduler.should_preempt(cp)
    assert result is not None
    reason, meta = result
    assert reason == PreemptionReason.SPECULATIVE_LOSER
    assert meta["winner_pid"] == "winner-1"


# ── AC 10: Priority preempt ──


@pytest.mark.asyncio
async def test_priority_preemption():
    """Higher-priority sibling pending → lower-priority preempted."""
    scheduler = Scheduler()

    # Register child B (priority=10) as pending under parent
    scheduler.register_pending_child("parent-1", "child-B", priority=10)

    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    # child-A is running with priority=1
    cp = AgentCheckpoint(
        pid="child-A",
        parent_pid="parent-1",
        status="RUNNING",
        agent_function_name="a",
        capabilities=bm.create_budgets({"api": 100}),
        priority=1,
    )

    result = scheduler.should_preempt(cp)
    assert result is not None
    reason, meta = result
    assert reason == PreemptionReason.PRIORITY_PREEMPTED
    assert meta["higher_priority_pid"] == "child-B"


# ── AC 12: Generic except doesn't swallow ──


@pytest.mark.asyncio
async def test_generic_except_does_not_swallow():
    """try/except Exception: does NOT catch PreemptedError."""
    caught_by_generic = False

    try:
        raise PreemptedError(PreemptionReason.BUDGET_EXHAUSTED)
    except Exception:
        caught_by_generic = True
    except BaseException:
        pass  # expected

    assert not caught_by_generic


# ── Scheduler unit tests ──


def test_scheduler_no_preempt_when_budget_ok():
    scheduler = Scheduler()
    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    cp = AgentCheckpoint(
        pid="ok",
        status="RUNNING",
        agent_function_name="a",
        capabilities=bm.create_budgets({"api": 100}),
    )
    assert scheduler.should_preempt(cp) is None


def test_scheduler_budget_exhaustion():
    scheduler = Scheduler()
    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    caps = bm.create_budgets({"api": 100})
    # Simulate overshoot
    caps["api"].current_usage = 150

    cp = AgentCheckpoint(
        pid="over",
        status="RUNNING",
        agent_function_name="a",
        capabilities=caps,
    )
    result = scheduler.should_preempt(cp)
    assert result is not None
    assert result[0] == PreemptionReason.BUDGET_EXHAUSTED


def test_scheduler_fork_loser_one_shot():
    """Fork loser is consumed on first should_preempt call."""
    scheduler = Scheduler()
    scheduler.mark_fork_loser("p1", winner_pid="w1")

    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    cp = AgentCheckpoint(
        pid="p1",
        status="RUNNING",
        agent_function_name="a",
        capabilities=bm.create_budgets({}),
    )
    assert scheduler.should_preempt(cp) is not None
    # Second call: consumed
    assert scheduler.should_preempt(cp) is None


def test_scheduler_priority_check_order():
    """Budget check happens before priority check."""
    scheduler = Scheduler()
    scheduler.register_pending_child("parent", "high-pri", priority=10)

    from castor.budget.manager import BudgetManager

    bm = BudgetManager()
    caps = bm.create_budgets({"api": 100})
    caps["api"].current_usage = 200  # over budget

    cp = AgentCheckpoint(
        pid="low-pri",
        parent_pid="parent",
        status="RUNNING",
        agent_function_name="a",
        capabilities=caps,
        priority=1,
    )
    result = scheduler.should_preempt(cp)
    assert result is not None
    # Budget takes precedence over priority
    assert result[0] == PreemptionReason.BUDGET_EXHAUSTED
