"""Paper A S-Pay matrix: C-full is effect-safe; ablations/baselines show dups."""

from __future__ import annotations

from castor.evals.actuator_bench import ActuatorBench
from castor.evals.paper_a.harness import run_s_pay_kill_trial
from castor.evals.paper_a.matrix import run_trial


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
