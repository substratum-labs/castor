"""External SQLite actuator used only by the ActuatorBench evaluation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ActuatorMetrics:
    """Observed effect-count metrics for one benchmark run."""

    committed_effects: int
    missing_effects: int
    dup_commits: int


class ActuatorBench:
    """A process-external effect sink backed by SQLite.

    When ``dedupe`` is true (default), ``operation_id`` is unique and retries
    return the original commit id (models ``C_actuator_dedup``).  When false,
    every ``commit`` inserts a new row so ablations can observe duplicates.
    """

    def __init__(self, db_path: Path, *, dedupe: bool = True) -> None:
        self._db_path = db_path
        self._dedupe = dedupe
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS commits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    effect_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            if dedupe:
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_commits_operation_id
                    ON commits(operation_id)
                    """
                )

    @property
    def dedupe(self) -> bool:
        return self._dedupe

    def commit(
        self, effect_name: str, payload: dict[str, object], *, operation_id: str
    ) -> dict[str, str]:
        """Persist an effect and return its external commit id."""
        commit_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._dedupe:
                connection.execute(
                    """
                    INSERT INTO commits (operation_id, commit_id, effect_name, payload_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(operation_id) DO NOTHING
                    """,
                    (
                        operation_id,
                        commit_id,
                        effect_name,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                stored_commit_id = connection.execute(
                    "SELECT commit_id FROM commits WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()[0]
            else:
                connection.execute(
                    """
                    INSERT INTO commits (operation_id, commit_id, effect_name, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        commit_id,
                        effect_name,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                stored_commit_id = commit_id

        return {"operation_id": operation_id, "commit_id": stored_commit_id}

    def list_commits(self) -> list[dict[str, str]]:
        """Return the external commit records in insertion order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT operation_id, commit_id, effect_name, payload_json
                FROM commits
                ORDER BY id
                """
            ).fetchall()

        return [
            {
                "operation_id": operation_id,
                "commit_id": commit_id,
                "effect_name": effect_name,
                "payload_json": payload_json,
            }
            for operation_id, commit_id, effect_name, payload_json in rows
        ]

    def metrics(self, expected_effects: int) -> ActuatorMetrics:
        """Return effect-count metrics without consulting Castor persistence."""
        with self._connect() as connection:
            committed_effects = connection.execute(
                "SELECT COUNT(*) FROM commits"
            ).fetchone()[0]

        return ActuatorMetrics(
            committed_effects=committed_effects,
            missing_effects=max(expected_effects - committed_effects, 0),
            dup_commits=max(committed_effects - expected_effects, 0),
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)
