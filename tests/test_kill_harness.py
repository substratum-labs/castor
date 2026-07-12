"""Integration coverage for the real-worker SIGKILL evaluation harness."""

from __future__ import annotations

from castor.evals.kill_harness import run_kill_after_commit


def test_kill_after_commit_recovers_without_duplicate_effect(tmp_path) -> None:
    """A post-commit SIGKILL is recovered through the durable actuator key."""
    result = run_kill_after_commit(tmp_path)

    assert result.committed_effects == 1
    assert result.dup_commits == 0
    assert result.missing_commits == 0
    assert result.resume_success is True
