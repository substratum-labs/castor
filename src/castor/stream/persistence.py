"""Checkpoint persistence to SQLite via SQLAlchemy."""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from castor.models.checkpoint import AgentCheckpoint


class CheckpointNotFoundError(Exception):
    """Raised when a checkpoint PID is not found in the store."""

    def __init__(self, pid: str):
        self.pid = pid
        super().__init__(f"Checkpoint not found: {pid!r}")


class Base(DeclarativeBase):
    pass


class CheckpointRow(Base):
    __tablename__ = "checkpoints"

    pid = Column(String, primary_key=True)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class WALRow(Base):
    __tablename__ = "wal_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pid = Column(String, nullable=False, index=True)
    syscall_index = Column(Integer, nullable=False)
    tool_name = Column(String, nullable=False)
    arguments = Column(Text, nullable=False)  # JSON
    budget_snapshot = Column(Text, nullable=False)  # JSON
    result = Column(Text, nullable=True)  # JSON, set on completion
    status = Column(String, nullable=False, default="PENDING")
    created_at = Column(DateTime, nullable=False)


class CheckpointStore:
    """Persist AgentCheckpoint to SQLite."""

    def __init__(self, db_url: str = "sqlite:///castor.db") -> None:
        self._engine = create_engine(db_url)
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)

    def save(self, checkpoint: AgentCheckpoint) -> None:
        """Serialize checkpoint to JSON and store in DB (upsert)."""
        with self._session_factory() as session:
            row = session.get(CheckpointRow, checkpoint.pid)
            now = datetime.now(UTC)
            if row:
                row.data = checkpoint.model_dump_json()
                row.updated_at = now
            else:
                row = CheckpointRow(
                    pid=checkpoint.pid,
                    data=checkpoint.model_dump_json(),
                    updated_at=now,
                )
                session.add(row)
            session.commit()

    def load(self, pid: str) -> AgentCheckpoint:
        """Load checkpoint from DB and deserialize."""
        with self._session_factory() as session:
            row = session.get(CheckpointRow, pid)
            if row is None:
                raise CheckpointNotFoundError(pid)
            return AgentCheckpoint.model_validate_json(row.data)

    def delete(self, pid: str) -> None:
        """Delete a checkpoint by PID."""
        with self._session_factory() as session:
            row = session.get(CheckpointRow, pid)
            if row:
                session.delete(row)
                session.commit()

    def list_pids(self) -> list[str]:
        """List all checkpoint PIDs."""
        with self._session_factory() as session:
            rows = session.query(CheckpointRow.pid).all()
            return [r.pid for r in rows]

    # ── WAL (Write-Ahead Log) ──

    def write_wal(
        self,
        pid: str,
        syscall_index: int,
        tool_name: str,
        arguments: dict,
        budget_snapshot: dict[str, float],
    ) -> None:
        """Write a PENDING WAL entry before tool execution."""
        with self._session_factory() as session:
            row = WALRow(
                pid=pid,
                syscall_index=syscall_index,
                tool_name=tool_name,
                arguments=_json.dumps(arguments),
                budget_snapshot=_json.dumps(budget_snapshot),
                status="PENDING",
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.commit()

    def complete_wal(self, pid: str, syscall_index: int, result: Any) -> None:
        """Mark a WAL entry as COMPLETED after successful execution."""
        with self._session_factory() as session:
            row = (
                session.query(WALRow)
                .filter_by(pid=pid, syscall_index=syscall_index, status="PENDING")
                .first()
            )
            if row:
                row.status = "COMPLETED"
                row.result = _json.dumps(result)
                session.commit()

    def list_pending_wal(self) -> list[dict]:
        """List all PENDING WAL entries."""
        with self._session_factory() as session:
            rows = session.query(WALRow).filter_by(status="PENDING").all()
            return [
                {
                    "pid": r.pid,
                    "syscall_index": r.syscall_index,
                    "tool_name": r.tool_name,
                    "arguments": _json.loads(r.arguments),
                    "budget_snapshot": _json.loads(r.budget_snapshot),
                    "status": r.status,
                }
                for r in rows
            ]

    def recover(self, pid: str) -> AgentCheckpoint | None:
        """Recover from crash: refund PENDING WAL entries, return patched checkpoint."""
        pending = [e for e in self.list_pending_wal() if e["pid"] == pid]
        if not pending:
            return None
        checkpoint = self.load(pid)
        for entry in pending:
            snapshot = entry["budget_snapshot"]
            for resource, usage_before in snapshot.items():
                if resource in checkpoint.capabilities:
                    checkpoint.capabilities[resource].current_usage = usage_before
        # Mark entries as ABANDONED
        with self._session_factory() as session:
            rows = session.query(WALRow).filter_by(pid=pid, status="PENDING").all()
            for row in rows:
                row.status = "ABANDONED"
            session.commit()
        self.save(checkpoint)
        return checkpoint

    def gc_wal(self) -> None:
        """Remove COMPLETED and ABANDONED WAL entries."""
        with self._session_factory() as session:
            session.query(WALRow).filter(
                WALRow.status.in_(["COMPLETED", "ABANDONED"])
            ).delete(synchronize_session="fetch")
            session.commit()
