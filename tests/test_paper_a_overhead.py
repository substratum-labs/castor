"""Tests for the Paper A T-257 negative control and overhead benchmark."""

from __future__ import annotations

import json

import pytest

from castor.evals.paper_a.harness import SPayHarnessResult, run_s_pay_kill_trial
from castor.evals.paper_a.overhead import (
    LScalingResult,
    run_l_scaling,
    write_overhead_results,
)


def test_s_bypass_repeats_raw_payment_after_crash(tmp_path):
    result = run_s_pay_kill_trial(
        tmp_path / "s_bypass",
        system="s_bypass",
        fault="kill_after_commit",
    )
    assert result.resume_success is True
    assert result.commits.count("payment") == 2
    assert result.dup_commits >= 1


def test_l_scaling_returns_one_completed_sample_per_length(tmp_path):
    samples = run_l_scaling(tmp_path / "scaling", lengths=(0, 2, 8))
    assert [sample.journal_len for sample in samples] == [0, 2, 8]
    assert all(sample.status == "COMPLETED" for sample in samples)
    assert all(sample.journal_bytes > 0 for sample in samples)
    assert all(sample.resume_ms >= 0 for sample in samples)
    assert all(sample.error is None for sample in samples)


def test_l_scaling_empty_lengths_returns_no_samples(tmp_path):
    assert run_l_scaling(tmp_path / "empty", lengths=()) == []


def test_l_scaling_rejects_negative_lengths(tmp_path):
    with pytest.raises(ValueError, match="non-negative"):
        run_l_scaling(tmp_path / "negative", lengths=(1, -1))


def test_write_overhead_results_writes_json_and_markdown(tmp_path):
    bypass = SPayHarnessResult(
        system="s_bypass",
        fault="kill_after_commit",
        committed_effects=3,
        dup_commits=1,
        missing_commits=0,
        resume_success=True,
        resumed_checkpoint_status="COMPLETED",
        commits=("payment", "payment", "email"),
    )
    scaling = [
        LScalingResult(
            journal_len=0,
            journal_bytes=256,
            resume_ms=1.5,
            status="COMPLETED",
            error=None,
        ),
        LScalingResult(
            journal_len=4,
            journal_bytes=512,
            resume_ms=2.5,
            status="COMPLETED",
            error=None,
        ),
    ]
    write_overhead_results(
        tmp_path / "artifacts",
        bypass=bypass,
        scaling=scaling,
        manifest={"command": "python -m castor.evals.paper_a.overhead"},
    )

    payload = json.loads(
        (tmp_path / "artifacts" / "overhead.json").read_text(encoding="utf-8")
    )
    assert payload["bypass"]["dup_commits"] == 1
    assert [row["journal_len"] for row in payload["l_scaling"]] == [0, 4]
    assert payload["manifest"]["command"] == (
        "python -m castor.evals.paper_a.overhead"
    )
    markdown = (tmp_path / "artifacts" / "overhead.md").read_text(encoding="utf-8")
    assert "S-Bypass" in markdown
    assert "journal_bytes" in markdown
