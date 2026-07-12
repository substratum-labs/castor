"""Minimal actuator seam for pending-commit reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class ActuatorStatus(StrEnum):
    """Status returned for a client operation known by an actuator."""

    COMMITTED = "COMMITTED"
    UNKNOWN = "UNKNOWN"


class InMemoryMockActuator:
    """Test actuator keyed by the provisional ``(pid, syscall_index)`` ID."""

    def __init__(
        self, statuses: Mapping[tuple[str, int], ActuatorStatus] | None = None
    ) -> None:
        self._statuses = dict(statuses or {})
        self.queried_ids: list[tuple[str, int]] = []

    def query_status(self, client_op_id: tuple[str, int]) -> str:
        self.queried_ids.append(client_op_id)
        return self._statuses.get(client_op_id, ActuatorStatus.UNKNOWN)
