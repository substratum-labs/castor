"""Checkpoint persistence to SQLite via SQLAlchemy."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, String, Text, create_engine
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
