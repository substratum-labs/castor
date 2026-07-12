"""Contract tests for the external SQLite ActuatorBench."""

from __future__ import annotations

import sqlite3

from castor.evals.actuator_bench import ActuatorBench


def test_commit_is_idempotent_for_a_repeated_operation_id(tmp_path) -> None:
    """A retried operation records one external effect with one commit id."""
    db_path = tmp_path / "actuator.sqlite3"
    actuator = ActuatorBench(db_path)

    first = actuator.commit("transfer", {"amount": 42}, operation_id="operation-001")
    retried = actuator.commit("transfer", {"amount": 42}, operation_id="operation-001")

    with sqlite3.connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0]

    metrics = actuator.metrics(expected_effects=1)

    assert retried["commit_id"] == first["commit_id"]
    assert row_count == 1
    assert metrics.dup_commits == 0
