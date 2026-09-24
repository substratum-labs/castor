"""Role-scoped conveniences over the untrusted AISA transport."""

from __future__ import annotations

import os
from typing import Any, ClassVar, Protocol

from .client import AisaClient, AisaProtocolError


class RequestTransport(Protocol):
    def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class _Session:
    allowed_operations: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, endpoint: str | os.PathLike[str] | RequestTransport) -> None:
        self._transport = (
            AisaClient(endpoint)
            if isinstance(endpoint, (str, os.PathLike))
            else endpoint
        )

    def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op not in self.allowed_operations:
            raise ValueError(f"{op} is unavailable on this AISA channel")
        response = self._transport.request(op, payload)
        outcome = response.get("outcome")
        if not isinstance(outcome, dict):
            raise AisaProtocolError(f"{op} returned no outcome object")
        return response


class AgentSession(_Session):
    """Thin client for the Roche Agent channel; Rust remains authoritative."""

    allowed_operations = frozenset(
        {
            "AdmitTurn",
            "CommitTurn",
            "RegisterAction",
            "PresentAdmissionCertificate",
            "RecordDispatchAttempt",
            "PersistFence",
            "RevokeCapability",
            "EnsureRegion",
            "RequestInteraction",
            "ReportOutcome",
            "ConsumeInteraction",
        }
    )


class OperatorSession(_Session):
    """Thin client for the host-only management channel."""

    allowed_operations = frozenset(
        {
            "GrantCapability",
            "RevokeCapability",
            "ResolveQuarantinedDispute",
            "PersistFence",
            "GetProjectionSummary",
            "InspectJournal",
            "SubmitDecision",
        }
    )
