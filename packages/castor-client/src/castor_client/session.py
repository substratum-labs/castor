"""Role-scoped conveniences over the untrusted AISA transport."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar, overload

from .client import AisaClient, AisaProtocolError
from .operations import AgentOperationRequest, OperatorOperationRequest

OutcomeT = TypeVar("OutcomeT", bound=Mapping[str, object])

AgentOperation = Literal[
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
]
OperatorOperation = Literal[
    "GrantCapability",
    "RevokeCapability",
    "ResolveQuarantinedDispute",
    "PersistFence",
    "GetProjectionSummary",
    "InspectJournal",
    "SubmitDecision",
]


@dataclass(frozen=True, slots=True)
class AgentRequest:
    op: AgentOperation
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OperatorRequest:
    op: OperatorOperation
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AisaResponse(Generic[OutcomeT]):
    request_id: str
    outcome: OutcomeT

    @property
    def outcome_type(self) -> str | None:
        value = self.outcome.get("type")
        return value if isinstance(value, str) else None


class RequestTransport(Protocol):
    def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class _Session:
    allowed_operations: ClassVar[frozenset[str]] = frozenset()
    untyped_read_operations: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        endpoint: str | os.PathLike[str] | RequestTransport,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(endpoint, (str, os.PathLike)):
            self._socket_path = os.fspath(endpoint)
            self._transport: RequestTransport | None = None
        else:
            self._socket_path = None
            self._transport = endpoint
        self._timeout_seconds = timeout_seconds

    def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op not in self.allowed_operations:
            raise ValueError(f"{op} is unavailable on this AISA channel")
        if self._socket_path is not None:
            client = AisaClient(self._socket_path, timeout=self._timeout_seconds)
            with client as transport:
                response = transport.request(op, payload)
        else:
            assert self._transport is not None
            response = self._transport.request(op, payload)
        if response.get("status") != "Ok":
            raise AisaProtocolError(f"{op} returned a non-Ok status")
        outcome = response.get("outcome")
        if not isinstance(outcome, dict):
            raise AisaProtocolError(f"{op} returned no outcome object")
        if op not in self.untyped_read_operations and (
            not isinstance(outcome.get("type"), str) or not outcome["type"]
        ):
            raise AisaProtocolError(f"{op} returned no outcome type")
        return response

    def _send(self, op: str, payload: dict[str, Any]) -> AisaResponse[dict[str, Any]]:
        response = self.request(op, payload)
        request_id = response.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise AisaProtocolError(f"{op} returned no request ID")
        return AisaResponse(request_id, response["outcome"])


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

    @overload
    def send(
        self, request: AgentOperationRequest[OutcomeT]
    ) -> AisaResponse[OutcomeT]: ...

    @overload
    def send(self, request: AgentRequest) -> AisaResponse[dict[str, Any]]: ...

    def send(
        self, request: AgentRequest | AgentOperationRequest[Any]
    ) -> AisaResponse[Any]:
        if isinstance(request, AgentOperationRequest):
            return self._send(request.op, request.to_payload())
        if isinstance(request, AgentRequest):
            return self._send(request.op, request.payload)
        raise TypeError("AgentSession.send requires an Agent request")


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
    untyped_read_operations = frozenset({"GetProjectionSummary", "InspectJournal"})

    @overload
    def send(
        self, request: OperatorOperationRequest[OutcomeT]
    ) -> AisaResponse[OutcomeT]: ...

    @overload
    def send(self, request: OperatorRequest) -> AisaResponse[dict[str, Any]]: ...

    def send(
        self, request: OperatorRequest | OperatorOperationRequest[Any]
    ) -> AisaResponse[Any]:
        if isinstance(request, OperatorOperationRequest):
            return self._send(request.op, request.to_payload())
        if isinstance(request, OperatorRequest):
            return self._send(request.op, request.payload)
        raise TypeError("OperatorSession.send requires an Operator request")
