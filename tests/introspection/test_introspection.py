"""Contract tests for the read-only ``sys_introspect`` journal syscall."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint, SyscallPurpose, SyscallRecord
from castor.models.introspection import (
    FindDecisionsQuery,
    FindSyscallQuery,
    GetReasoningChainQuery,
    GetSyscallQuery,
    IntrospectionTargetNotFoundError,
    SummarizeQuery,
)
from castor.scheduler.introspection import IntrospectionEngine
from castor.scheduler.proxy import SyscallProxy


def record(
    name: str,
    response: object,
    *,
    purpose: SyscallPurpose = SyscallPurpose.TASK_EXECUTION,
    arguments: dict[str, object] | None = None,
    invocation_id: str | None = None,
) -> SyscallRecord:
    return SyscallRecord(
        request={"tool_name": name, "arguments": arguments or {}},
        response=response,
        purpose=purpose,
        invocation_id=invocation_id,
    )


@pytest.fixture
def journal() -> list[SyscallRecord]:
    return [
        record("llm_inference", "hypothesis alpha", invocation_id="llm-0"),
        record("mem_write", "stored", purpose=SyscallPurpose.MEMORY_MANAGEMENT),
        record("search", "alpha evidence", invocation_id="search-2"),
        record("llm_inference", "approve alpha", invocation_id="llm-3"),
        record("deploy", "ok", arguments={"plan": "hypothesis alpha"}),
    ]


@pytest.fixture
def engine() -> IntrospectionEngine:
    return IntrospectionEngine()


def test_find_syscall_filters_and_marks_truncation(engine, journal):
    result = engine.execute(
        FindSyscallQuery(purpose=SyscallPurpose.TASK_EXECUTION, limit=2), journal
    )

    assert [snapshot.syscall_index for snapshot in result.payload.matches] == [0, 2]
    assert result.payload.truncated is True


def test_find_syscall_filters_by_name_range_cost_and_duration(engine, journal):
    result = engine.execute(
        FindSyscallQuery(
            syscall_name="llm_inference",
            step_range=(1, 4),
            cost_min=0,
            duration_ms_min=0,
        ),
        journal,
    )

    assert [snapshot.syscall_index for snapshot in result.payload.matches] == [3]


def test_get_syscall_resolves_index_and_invocation_id(engine, journal):
    by_index = engine.execute(GetSyscallQuery(target=2), journal)
    by_id = engine.execute(GetSyscallQuery(target="llm-3"), journal)

    assert by_index.payload.snapshot.name == "search"
    assert by_id.payload.snapshot.syscall_index == 3


def test_get_syscall_rejects_missing_target(engine, journal):
    with pytest.raises(IntrospectionTargetNotFoundError):
        engine.execute(GetSyscallQuery(target="absent"), journal)


def test_snapshots_digest_and_truncate_output_unless_opted_in(engine):
    output = "x" * 5000
    journal = [record("llm_inference", output, invocation_id="large")]

    truncated = engine.execute(GetSyscallQuery(target="large"), journal)
    full = engine.execute(
        GetSyscallQuery(target="large", include_full_output=True), journal
    )

    assert truncated.payload.snapshot.output_summary.endswith("…")
    assert (
        truncated.payload.snapshot.output_digest
        == hashlib.sha256(output.encode()).hexdigest()
    )
    assert full.payload.snapshot.output_summary == output


def test_reasoning_chain_includes_prior_llm_output_referenced_by_target(
    engine, journal
):
    result = engine.execute(GetReasoningChainQuery(target_step=4), journal)

    assert [snapshot.syscall_index for snapshot in result.payload.chain] == [0, 4]


def test_reasoning_chain_honors_max_depth(engine):
    journal = [
        record("llm_inference", "a", invocation_id="a"),
        record("llm_inference", "a", invocation_id="b"),
        record("deploy", "ok", arguments={"plan": "a"}),
    ]

    result = engine.execute(GetReasoningChainQuery(target_step=2, max_depth=1), journal)

    assert [snapshot.syscall_index for snapshot in result.payload.chain] == [2]
    assert result.payload.truncated_at_max_depth is True


def test_summarize_reports_totals_errors_and_groups(engine, journal):
    journal[2].response = {"status": "ERROR"}
    result = engine.execute(SummarizeQuery(group_by="purpose"), journal)

    assert result.payload.total_syscalls == 5
    assert result.payload.error_count == 1
    assert result.payload.by_group["task_execution"]["count"] == 4
    assert result.payload.by_group["memory_management"]["count"] == 1


def test_find_decisions_matches_llm_outputs_only(engine, journal):
    result = engine.execute(FindDecisionsQuery(output_pattern="alpha"), journal)

    assert [snapshot.syscall_index for snapshot in result.payload.matches] == [0, 3]


def test_invalid_regex_is_rejected(engine, journal):
    with pytest.raises(ValueError, match="invalid output_pattern"):
        engine.execute(FindDecisionsQuery(output_pattern="["), journal)


def test_timeout_returns_partial_result_with_last_processed_step():
    ticks: Iterator[float] = iter([0.0, 0.2, 0.3])
    engine = IntrospectionEngine(clock=lambda: next(ticks))

    result = engine.execute(SummarizeQuery(), [record("search", "ok")], deadline_ms=100)

    assert result.payload.type == "partial"
    assert result.payload.timeout_at_step == 0


async def test_proxy_records_introspection_cost_and_replays_result(journal):
    budget_mgr = BudgetManager()
    checkpoint = AgentCheckpoint(
        pid="self-debug-1",
        status="RUNNING",
        agent_function_name="agent",
        capabilities=budget_mgr.create_budgets({"api_usd": 1.0}),
        syscall_log=journal,
    )
    proxy = SyscallProxy(checkpoint, SyscallGate(ToolRegistry()), budget_mgr)
    proxy._replay_index = len(journal)

    before = [item.model_dump() for item in checkpoint.syscall_log]
    result = await proxy.introspect(SummarizeQuery())

    assert result.payload.total_syscalls == 5
    assert [item.model_dump() for item in checkpoint.syscall_log[:-1]] == before
    assert checkpoint.syscall_log[-1].purpose == SyscallPurpose.INTROSPECTION
    assert checkpoint.syscall_log[-1].cost == pytest.approx(0.0001)
    assert checkpoint.capabilities["api_usd"].current_usage == pytest.approx(0.0001)

    replay = SyscallProxy(checkpoint, SyscallGate(ToolRegistry()), budget_mgr)
    replay._replay_index = len(checkpoint.syscall_log) - 1
    replayed = await replay.introspect(SummarizeQuery())
    assert replayed == result
