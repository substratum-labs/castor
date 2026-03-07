"""Checkpoint persistence: Protocol, in-memory store, and SQLite store."""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from castor.models.checkpoint import AgentCheckpoint
from castor.observability import get_logger

_logger = get_logger("castor.persistence")


class CheckpointNotFoundError(Exception):
    """Raised when a checkpoint PID is not found in the store."""

    def __init__(self, pid: str):
        self.pid = pid
        super().__init__(f"Checkpoint not found: {pid!r}")


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class CheckpointStoreProtocol(Protocol):
    """Minimal interface that any checkpoint store must implement."""

    def save(self, checkpoint: AgentCheckpoint) -> None: ...
    def load(self, pid: str) -> AgentCheckpoint: ...
    def delete(self, pid: str) -> None: ...
    def list_pids(self) -> list[str]: ...


# ── In-memory store (for testing) ────────────────────────────────────────────


class MemoryCheckpointStore:
    """Dict-backed checkpoint store. No external dependencies."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}  # pid → JSON string

    def save(self, checkpoint: AgentCheckpoint) -> None:
        self._store[checkpoint.pid] = checkpoint.model_dump_json()

    def load(self, pid: str) -> AgentCheckpoint:
        data = self._store.get(pid)
        if data is None:
            raise CheckpointNotFoundError(pid)
        return AgentCheckpoint.model_validate_json(data)

    def delete(self, pid: str) -> None:
        self._store.pop(pid, None)

    def list_pids(self) -> list[str]:
        return list(self._store.keys())


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

    def list_by_parent(self, parent_pid: str) -> list[AgentCheckpoint]:
        """List all checkpoints with the given parent_pid."""
        with self._session_factory() as session:
            rows = session.query(CheckpointRow).all()
            results = []
            for row in rows:
                cp = AgentCheckpoint.model_validate_json(row.data)
                if cp.parent_pid == parent_pid:
                    results.append(cp)
            return results

    def gc_orphans(self) -> list[str]:
        """Mark orphaned children (parent done, child still RUNNING) as FAILED."""
        orphaned: list[str] = []
        with self._session_factory() as session:
            all_rows = session.query(CheckpointRow).all()
            checkpoints = {
                r.pid: AgentCheckpoint.model_validate_json(r.data) for r in all_rows
            }
            for pid, cp in checkpoints.items():
                if cp.parent_pid and cp.status == "RUNNING":
                    parent = checkpoints.get(cp.parent_pid)
                    if parent is None or parent.status in (
                        "COMPLETED",
                        "FAILED",
                    ):
                        if parent is None:
                            _logger.warning(
                                "gc_orphan_missing_parent",
                                extra={
                                    "pid": pid,
                                    "parent_pid": cp.parent_pid,
                                },
                            )
                        cp.status = "FAILED"
                        cp.preemption_reason = "ORPHANED"
                        self.save(cp)
                        orphaned.append(pid)
        return orphaned

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
            if row is None:
                _logger.warning(
                    "wal_complete_miss",
                    extra={"pid": pid, "syscall_index": syscall_index},
                )
                return
            row.status = "COMPLETED"
            row.result = _json.dumps(result)
            session.commit()

    def abandon_wal(self, pid: str, syscall_index: int) -> None:
        """Mark a WAL entry as ABANDONED (tool failed or timed out)."""
        with self._session_factory() as session:
            row = (
                session.query(WALRow)
                .filter_by(pid=pid, syscall_index=syscall_index, status="PENDING")
                .first()
            )
            if row:
                row.status = "ABANDONED"
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
        try:
            checkpoint = self.load(pid)
        except CheckpointNotFoundError:
            _logger.warning(
                "wal_recover_no_checkpoint",
                extra={"pid": pid, "pending_count": len(pending)},
            )
            # Abandon orphaned WAL entries — no checkpoint to recover
            with self._session_factory() as session:
                rows = session.query(WALRow).filter_by(pid=pid, status="PENDING").all()
                for row in rows:
                    row.status = "ABANDONED"
                session.commit()
            return None
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
