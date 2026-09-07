"""Isolated external SQLite actuator endpoint for cognitive recovery verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

SECRET_KEY = b"test-only-actuator-receipt-key-not-for-production"
SIGNING_KEY = SECRET_KEY
ISSUER = "fixture-evidence-service"
ADAPTER = "c04:generic"
DEFAULT_OP_ID = "recovery-payment-1"
DEFAULT_SCOPE = "payment:fixture:merchant-42"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class RecoveryActuator:
    """External SQLite endpoint representing an external payment/effect provider.

    Completely isolated from castord's journal and worker memory.
    Provides three authoritative states:
    1. 'Committed' (executed receipt)
    2. 'not_found' (non-terminal / in-flight / unknown)
    3. 'TerminatedRejected' (atomic tombstone rejecting delayed arrivals)
    """

    def __init__(self, path: Path | str, secret_key: bytes = SECRET_KEY):
        self.path = Path(path)
        self.secret_key = secret_key
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations (op TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS commits (op TEXT PRIMARY KEY)")

    def query(self, op_id: str = DEFAULT_OP_ID) -> str:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT state FROM operations WHERE op=?", (op_id,)
            ).fetchone()
        return row[0] if row else "not_found"

    def cancel(self, op_id: str = DEFAULT_OP_ID) -> dict[str, Any]:
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM operations WHERE op=?", (op_id,)
            ).fetchone()
            assert row is None or row[0] == "TerminatedRejected", (
                "cannot tombstone an already committed operation"
            )
            db.execute(
                "INSERT OR REPLACE INTO operations VALUES (?, 'TerminatedRejected')",
                (op_id,),
            )
        return self.receipt(resolution="NotApplied", op_id=op_id)

    def arrive(self, op_id: str = DEFAULT_OP_ID) -> str:
        """Simulate delayed physical execution arrival in a separate process."""
        code = """import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT state FROM operations WHERE op=?", (sys.argv[2],)).fetchone()
    if row is None:
        db.execute("INSERT INTO operations VALUES (?, 'Committed')", (sys.argv[2],))
        db.execute("INSERT INTO commits VALUES (?)", (sys.argv[2],))
        print("Committed")
    else:
        print(row[0])
"""
        return subprocess.check_output(
            [sys.executable, "-c", code, str(self.path), op_id], text=True, timeout=5
        ).strip()

    def count(self) -> int:
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT count(*) FROM commits").fetchone()[0]

    def receipt(
        self,
        resolution: str = "Confirmed",
        attempt_id: int = 1,
        op_id: str = DEFAULT_OP_ID,
        scope: str = DEFAULT_SCOPE,
        adapter_id: str = ADAPTER,
    ) -> dict[str, Any]:
        body = {
            "attempt_id": attempt_id,
            "stable_operation_id": op_id,
            "adapter_id": adapter_id,
            "issuer": ISSUER,
            "request_digest": scope,
            "settlement_schema_version": 1,
            "resolution": resolution,
            "actuator_state": self.query(op_id),
        }
        raw_canonical = canonical_json(body)
        signature = hmac.new(self.secret_key, raw_canonical, hashlib.sha256).hexdigest()
        return {
            **body,
            "signature": signature,
        }
