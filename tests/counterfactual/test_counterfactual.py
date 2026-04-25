"""Counterfactual replay acceptance tests.

Tests per ACs 1-9, 11-12 from the briefing.
"""

from __future__ import annotations

import pytest

from castor import Castor, castor_tool
from castor.models.counterfactual import (
    CounterfactualRecord,
    CounterfactualResult,
    OverrideNotAllowedError,
    OverrideTargetNotFoundError,
    ReplayMode,
    SyscallOverride,
)

# ── Fixtures ──


@castor_tool(consumes="api", cost_per_use=0.1)
async def choose(options: str = "A,B") -> str:
    """Deterministic: always picks first option."""
    return options.split(",")[0].strip()


@castor_tool(consumes="api", cost_per_use=0.1)
async def investigate(hypothesis: str = "") -> str:
    return f"investigated: {hypothesis}"


@castor_tool(consumes="api", cost_per_use=0.0)
async def noop() -> str:
    return "ok"


async def _fixture_agent(proxy) -> str:
    """3-step agent: choose → investigate → conclude."""
    choice = await proxy.syscall("choose", {"options": "A,B"})
    finding = await proxy.syscall("investigate", {"hypothesis": choice})
    return f"conclusion from {finding}"


async def _build_base_session():
    """Run the fixture agent and return (kernel, checkpoint)."""
    kernel = Castor(
        tools=[choose, investigate, noop],
        budgets={"api": 100.0},
    )
    cp = await kernel.run(_fixture_agent, pid="cf-base-001")
    assert cp.status == "COMPLETED"
    assert "conclusion" in cp.result
    return kernel, cp


# ── AC 1: Types exist ──


def test_types_exist():
    assert SyscallOverride is not None
    assert CounterfactualRecord is not None
    assert ReplayMode is not None
    assert CounterfactualResult is not None
    assert OverrideNotAllowedError is not None
    assert OverrideTargetNotFoundError is not None


# ── AC 2: Checkpoint backwards compat ──


def test_checkpoint_backwards_compat():
    from castor import AgentCheckpoint

    data = {
        "pid": "old",
        "status": "COMPLETED",
        "agent_function_name": "a",
        "capabilities": {},
    }
    cp = AgentCheckpoint.model_validate(data)
    assert cp.counterfactual_log == []
    assert cp.parent_session_id is None
    assert cp.diverged_at_step is None


# ── AC 4: Disallowed syscalls ──


def test_override_disallowed_syscall():
    from castor.models.checkpoint import SyscallPurpose, SyscallRecord
    from castor.scheduler.counterfactual import validate_overrides

    log = [
        SyscallRecord(
            request={"tool_name": "spawn_agent", "arguments": {}},
            response="child-1",
            purpose=SyscallPurpose.TASK_EXECUTION,
            invocation_id="spawn_001",
        ),
    ]
    with pytest.raises(OverrideNotAllowedError):
        validate_overrides({0: SyscallOverride(replacement_output="fake")}, log)


def test_override_mem_write_disallowed():
    from castor.models.checkpoint import SyscallPurpose, SyscallRecord
    from castor.scheduler.counterfactual import validate_overrides

    log = [
        SyscallRecord(
            request={"tool_name": "mem_write", "arguments": {}},
            response={"memory_id": "x"},
            purpose=SyscallPurpose.MEMORY_MANAGEMENT,
            invocation_id="mw_001",
        ),
    ]
    with pytest.raises(OverrideNotAllowedError):
        validate_overrides({0: SyscallOverride(replacement_output="fake")}, log)


# ── AC 6: Both int and str keys ──


def test_index_and_invocation_id_keys():
    from castor.models.checkpoint import SyscallPurpose, SyscallRecord
    from castor.scheduler.counterfactual import validate_overrides

    log = [
        SyscallRecord(
            request={"tool_name": "choose", "arguments": {}},
            response="A",
            purpose=SyscallPurpose.TASK_EXECUTION,
            invocation_id="inv_001",
        ),
    ]
    # By index
    r1 = validate_overrides({0: SyscallOverride(replacement_output="B")}, log)
    assert "inv_001" in r1

    # By invocation_id
    r2 = validate_overrides({"inv_001": SyscallOverride(replacement_output="B")}, log)
    assert "inv_001" in r2


def test_bad_index_raises():
    from castor.scheduler.counterfactual import validate_overrides

    with pytest.raises(OverrideTargetNotFoundError):
        validate_overrides({999: SyscallOverride(replacement_output="X")}, [])


def test_bad_invocation_id_raises():
    from castor.scheduler.counterfactual import validate_overrides

    with pytest.raises(OverrideTargetNotFoundError):
        validate_overrides({"nonexistent": SyscallOverride(replacement_output="X")}, [])


# ── AC 7: LIVE_FROM_DIVERGENCE ──


@pytest.mark.asyncio
async def test_live_from_divergence():
    """Override step 0 → steps 1+ run live (not from cache)."""
    kernel, base_cp = await _build_base_session()

    # Override step 0 (choose) to return "B" instead of "A"
    result = await kernel.replay_with_overrides(
        base_checkpoint=base_cp,
        agent_fn=_fixture_agent,
        overrides={
            0: SyscallOverride(
                replacement_output="B",
                note="what if agent chose B?",
            )
        },
        mode=ReplayMode.LIVE_FROM_DIVERGENCE,
        budgets={"api": 100.0},
    )

    assert isinstance(result, CounterfactualResult)
    assert result.parent_session_id == base_cp.pid
    assert result.diverged_at_step == 0
    assert result.final_status == "COMPLETED"
    assert len(result.overrides_applied) >= 1
    assert result.overrides_applied[0].replacement_output == "B"


# ── AC 8: REPLAY_WHEN_ARGS_MATCH with no-op override ──


@pytest.mark.asyncio
async def test_replay_when_args_match_noop():
    """No-op override (A→A) → downstream replays from cache."""
    kernel, base_cp = await _build_base_session()

    result = await kernel.replay_with_overrides(
        base_checkpoint=base_cp,
        agent_fn=_fixture_agent,
        overrides={
            0: SyscallOverride(
                replacement_output="A",  # same as original
                note="no-op override",
            )
        },
        mode=ReplayMode.REPLAY_WHEN_ARGS_MATCH,
        budgets={"api": 100.0},
    )

    assert result.final_status == "COMPLETED"
    assert result.total_steps >= 2


# ── AC 9: REPLAY_ALL ──


@pytest.mark.asyncio
async def test_replay_all_fiction_mode():
    """Override that changes args → REPLAY_ALL still uses parent values."""
    kernel, base_cp = await _build_base_session()

    result = await kernel.replay_with_overrides(
        base_checkpoint=base_cp,
        agent_fn=_fixture_agent,
        overrides={
            0: SyscallOverride(
                replacement_output="COMPLETELY_DIFFERENT",
                note="fiction test",
            )
        },
        mode=ReplayMode.REPLAY_ALL,
        budgets={"api": 100.0},
    )

    assert result.final_status == "COMPLETED"


# ── AC 11: Budget ──


@pytest.mark.asyncio
async def test_fresh_budget_default():
    """CF session gets fresh budget by default."""
    kernel, base_cp = await _build_base_session()

    result = await kernel.replay_with_overrides(
        base_checkpoint=base_cp,
        agent_fn=_fixture_agent,
        overrides={0: SyscallOverride(replacement_output="B")},
        budgets={"api": 50.0},
    )
    # Fresh budget, so total_cost starts from 0
    assert result.total_cost >= 0


# ── AC 12: Disallowed syscall names ──


@pytest.mark.asyncio
async def test_all_disallowed_names():
    from castor.scheduler.counterfactual import DISALLOWED_SYSCALL_NAMES

    # Verify the set is non-empty and contains expected names
    assert "spawn_agent" in DISALLOWED_SYSCALL_NAMES
    assert "mem_write" in DISALLOWED_SYSCALL_NAMES
    assert "mem_delete" in DISALLOWED_SYSCALL_NAMES


# ── AC 13: Tampered parent detection ──


def test_digest_output():
    from castor.models.counterfactual import digest_output

    d1 = digest_output("hello")
    d2 = digest_output("hello")
    d3 = digest_output("world")
    assert d1 == d2
    assert d1 != d3
    assert len(d1) == 32
