"""Paper A S-Pay matrix: C-full is effect-safe; ablations/baselines show dups."""

from __future__ import annotations

import json
from pathlib import Path

from castor.evals.actuator_bench import ActuatorBench
from castor.evals.paper_a.harness import run_s_pay_kill_trial
from castor.evals.paper_a.matrix import run_matrix, run_trial, write_results
from castor.evals.paper_a.secondary_workloads import (
    run_s_hitl_workload,
    run_s_loop_workload,
)


def test_langgraph_kill_after_commit_reenters_uncheckpointed_payment(tmp_path) -> None:
    result = run_s_pay_kill_trial(
        tmp_path / "langgraph_commit",
        system="b_langgraph",
        fault="kill_after_commit",
    )
    assert result.resume_success is True
    assert result.committed_effects == 3
    assert result.dup_commits == 1
    assert result.missing_commits == 0
    assert result.commits.count("payment") == 2
    assert result.commits.count("email") == 1


def test_langgraph_kill_after_success_keeps_checkpointed_payment(tmp_path) -> None:
    result = run_s_pay_kill_trial(
        tmp_path / "langgraph_success",
        system="b_langgraph",
        fault="kill_after_success",
    )
    assert result.resume_success is True
    assert result.committed_effects == 2
    assert result.dup_commits == 0
    assert result.missing_commits == 0
    assert result.commits.count("payment") == 1
    assert result.commits.count("email") == 1


def test_actuator_no_dedupe_allows_duplicate_operation_ids(tmp_path) -> None:
    actuator = ActuatorBench(tmp_path / "a.sqlite3", dedupe=False)
    actuator.commit("payment", {"amount": 1}, operation_id="same")
    actuator.commit("payment", {"amount": 1}, operation_id="same")
    metrics = actuator.metrics(expected_effects=1)
    assert metrics.committed_effects == 2
    assert metrics.dup_commits == 1


def test_c_full_kill_after_commit_zero_dups(tmp_path) -> None:
    result = run_s_pay_kill_trial(
        tmp_path / "c_full_commit",
        system="c_full",
        fault="kill_after_commit",
    )
    assert result.resume_success is True
    assert result.dup_commits == 0
    assert result.missing_commits == 0
    assert result.committed_effects == 2
    assert result.commits.count("payment") == 1
    assert result.commits.count("email") == 1


def test_c_full_kill_after_success_zero_dups(tmp_path) -> None:
    result = run_s_pay_kill_trial(
        tmp_path / "c_full_success",
        system="c_full",
        fault="kill_after_success",
    )
    assert result.resume_success is True
    assert result.dup_commits == 0
    assert result.committed_effects == 2


def test_b_naive_kill_after_commit_duplicates_payment(tmp_path) -> None:
    result = run_s_pay_kill_trial(
        tmp_path / "naive",
        system="b_naive",
        fault="kill_after_commit",
    )
    assert result.resume_success is True
    assert result.dup_commits >= 1
    assert result.commits.count("payment") >= 2


def test_c_no_op_id_kill_after_commit_duplicates_payment(tmp_path) -> None:
    result = run_s_pay_kill_trial(
        tmp_path / "no_op",
        system="c_no_op_id",
        fault="kill_after_commit",
    )
    assert result.resume_success is True
    # DIE_AFTER_COMMIT re-enters tool; unstable op_id → second payment row.
    assert result.commits.count("payment") >= 2
    assert result.dup_commits >= 1


def test_c_no_op_id_kill_after_success_zero_payment_dups_via_journal(
    tmp_path,
) -> None:
    """After durable journal append, catch-up must not re-enter payment.

    Unstable op_ids would duplicate if the tool body re-ran; journal cache
    alone must prevent a second payment when the crash is post-success.
    """
    result = run_s_pay_kill_trial(
        tmp_path / "no_op_success",
        system="c_no_op_id",
        fault="kill_after_success",
    )
    assert result.resume_success is True
    assert result.commits.count("payment") == 1
    assert result.dup_commits == 0
    assert result.committed_effects == 2


def test_matrix_trial_records_c_full(tmp_path) -> None:
    trial = run_trial(
        tmp_path / "matrix",
        system="c_full",
        fault="kill_after_success",
        trial=0,
    )
    assert trial.error is None
    assert trial.dup_commits == 0
    assert trial.resume_success is True


def test_write_results_keeps_trial_rows_and_writes_manifest(tmp_path) -> None:
    results = run_matrix(
        tmp_path / "work",
        systems=("c_full",),
        faults=("kill_after_success",),
        trials=1,
    )
    write_results(
        tmp_path / "artifacts",
        results,
        manifest={
            "label": "test-smoke",
            "trials": 1,
            "systems": ["c_full"],
            "faults": ["kill_after_success"],
            "result_count": 1,
            "command": "python -m castor.evals.paper_a.matrix --label test-smoke",
            "git_commit": "deadbeef",
            "generated_at_utc": "2026-07-13T00:00:00+00:00",
        },
    )
    assert isinstance(
        json.loads((tmp_path / "artifacts/results.json").read_text()), list
    )
    assert json.loads((tmp_path / "artifacts/run_manifest.json").read_text()) == {
        "faults": ["kill_after_success"],
        "command": "python -m castor.evals.paper_a.matrix --label test-smoke",
        "generated_at_utc": "2026-07-13T00:00:00+00:00",
        "git_commit": "deadbeef",
        "label": "test-smoke",
        "result_count": 1,
        "systems": ["c_full"],
        "trials": 1,
    }


def test_s_hitl_approve_has_one_payment_and_no_duplicates(tmp_path) -> None:
    result = run_s_hitl_workload(tmp_path, decision="approve")
    assert result.checkpoint_status == "COMPLETED"
    assert result.committed_effects == 1
    assert result.dup_commits == 0


def test_s_hitl_reject_executes_no_payment(tmp_path) -> None:
    result = run_s_hitl_workload(tmp_path, decision="reject")
    assert result.checkpoint_status == "COMPLETED"
    assert result.committed_effects == 0
    assert result.journal_statuses[-1] == "HITL_REJECTED"


def test_s_loop_stops_at_budget_without_extra_effect() -> None:
    result = run_s_loop_workload()
    assert result.checkpoint_status == "COMPLETED"
    assert result.committed_effects == 1
    assert result.journal_statuses[-1] == "INSUFFICIENT_CAPABILITY"


def test_matrix_records_langgraph_in_existing_result_schema(tmp_path) -> None:
    trial = run_trial(
        tmp_path / "matrix",
        system="b_langgraph",
        fault="kill_after_commit",
        trial=0,
    )
    assert trial.error is None
    assert trial.system == "b_langgraph"
    assert set(trial.__dict__) == {
        "system",
        "fault",
        "trial",
        "committed_effects",
        "dup_commits",
        "missing_commits",
        "resume_success",
        "resumed_checkpoint_status",
        "commits",
        "wall_ms",
        "error",
    }
    assert trial.dup_commits == 1


def test_langgraph_comparison_note_states_the_fairness_boundary() -> None:
    note = Path("docs/paper_a/langgraph_baseline.md").read_text(encoding="utf-8")
    for required in (
        "LangGraph 1.1.2",
        "SQLite",
        "tool cache: off",
        "no stable external operation_id",
        "kill_after_commit",
        "not a universal claim",
    ):
        assert required in note
