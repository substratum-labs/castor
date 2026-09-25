"""Operation-specific D1 request shapes for the Rust AISA gateway.

These objects describe the wire payload. They do not validate capabilities or
grant authority; castord performs all semantic checks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import ClassVar, Generic, Literal, TypedDict, TypeVar

from .outcomes import (
    AdmitTurnOutcome,
    CommitTurnOutcome,
    ConsumeInteractionOutcome,
    GrantCapabilityOutcome,
    JournalInspection,
    PersistFenceOutcome,
    PresentAdmissionOutcome,
    ProjectionSummary,
    RecordDispatchOutcome,
    RegionOutcome,
    RegisterActionOutcome,
    ReportOutcomeResult,
    RequestInteractionOutcome,
    ResolveDisputeOutcome,
    RevokeCapabilityOutcome,
    SubmitDecisionOutcome,
)

OutcomeT = TypeVar("OutcomeT")


class ActionBinding(TypedDict):
    action_id: str
    payload_region_ref: str
    payload_digest: str
    actuator_id: str


class QueryOperationDescriptor(TypedDict):
    type: Literal["QueryOperation"]
    attempt_id: int
    stable_operation_id: str
    adapter_id: str


class ExactMatchFields(TypedDict):
    key: str
    value: str


class ExactMatchConstraint(TypedDict):
    ExactMatch: ExactMatchFields


class ScopePrefixFields(TypedDict):
    prefix: str


class ScopePrefixConstraint(TypedDict):
    ScopePrefix: ScopePrefixFields


class NumericUpperBoundFields(TypedDict):
    metric: str
    limit: int


class NumericUpperBoundConstraint(TypedDict):
    NumericUpperBound: NumericUpperBoundFields


Constraint = ExactMatchConstraint | ScopePrefixConstraint | NumericUpperBoundConstraint


class CapabilityGrant(TypedDict):
    cap_id: str
    subject: str
    object_ref: str
    rights: list[Literal["AdmitTurn", "RegisterAction", "Revoke", "Derive"]]
    constraints: list[Constraint]
    parent_cap_id: str | None
    revocation_domain: str | None
    delegation_allowed: bool
    max_turns: int | None


class WireOperation:
    op: ClassVar[str]

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


class AgentOperationRequest(WireOperation, Generic[OutcomeT]):
    """Marker for an operation permitted on the Agent channel."""


class OperatorOperationRequest(WireOperation, Generic[OutcomeT]):
    """Marker for an operation permitted on the control channel."""


@dataclass(frozen=True, slots=True)
class AdmitTurn(AgentOperationRequest[AdmitTurnOutcome]):
    op: ClassVar[Literal["AdmitTurn"]] = "AdmitTurn"
    agent_id: str
    turn_id: int
    lease_epoch: int
    base_projection_digest: str
    cap_id: str | None = None


@dataclass(frozen=True, slots=True)
class RequestInteraction(AgentOperationRequest[RequestInteractionOutcome]):
    op: ClassVar[Literal["RequestInteraction"]] = "RequestInteraction"
    interaction_id: str
    lease_epoch: int
    request_digest: str
    descriptor: QueryOperationDescriptor | None = None

    def to_payload(self) -> dict[str, object]:
        payload = super().to_payload()
        if self.descriptor is None:
            payload.pop("descriptor")
        return payload


@dataclass(frozen=True, slots=True)
class ReportOutcome(AgentOperationRequest[ReportOutcomeResult]):
    op: ClassVar[Literal["ReportOutcome"]] = "ReportOutcome"
    interaction_id: str
    observation_region_id: str
    observation_digest: str


@dataclass(frozen=True, slots=True)
class ConsumeInteraction(AgentOperationRequest[ConsumeInteractionOutcome]):
    op: ClassVar[Literal["ConsumeInteraction"]] = "ConsumeInteraction"
    interaction_id: str
    lease_epoch: int


@dataclass(frozen=True, slots=True)
class CommitTurn(AgentOperationRequest[CommitTurnOutcome]):
    op: ClassVar[Literal["CommitTurn"]] = "CommitTurn"
    lease_epoch: int
    base_projection_digest: str
    successor_region_id: str
    successor_digest: str
    action_manifest_region_id: str
    action_manifest_digest: str
    action_manifest: list[str]
    action_bindings: list[ActionBinding]
    cap_id: str | None = None


@dataclass(frozen=True, slots=True)
class RegisterAction(AgentOperationRequest[RegisterActionOutcome]):
    op: ClassVar[Literal["RegisterAction"]] = "RegisterAction"
    action_id: str
    agent_id: str
    action_family: str
    cap_id: str
    target_scope: str
    stable_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class PresentAdmissionCertificate(AgentOperationRequest[PresentAdmissionOutcome]):
    op: ClassVar[Literal["PresentAdmissionCertificate"]] = "PresentAdmissionCertificate"
    action_id: str
    target_scope: str
    capability_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class RecordDispatchAttempt(AgentOperationRequest[RecordDispatchOutcome]):
    op: ClassVar[Literal["RecordDispatchAttempt"]] = "RecordDispatchAttempt"
    attempt_id: int
    dispatch_identity: str


@dataclass(frozen=True, slots=True)
class AgentPersistFence(AgentOperationRequest[PersistFenceOutcome]):
    op: ClassVar[Literal["PersistFence"]] = "PersistFence"
    generation: int


@dataclass(frozen=True, slots=True)
class AgentRevokeCapability(AgentOperationRequest[RevokeCapabilityOutcome]):
    op: ClassVar[Literal["RevokeCapability"]] = "RevokeCapability"
    capability_id: str
    authorization_capability_id: str


@dataclass(frozen=True, slots=True)
class EnsureRegion(AgentOperationRequest[RegionOutcome]):
    op: ClassVar[Literal["EnsureRegion"]] = "EnsureRegion"
    region_ref: str
    content_digest: str
    content: list[int]


@dataclass(frozen=True, slots=True)
class GrantCapability(OperatorOperationRequest[GrantCapabilityOutcome]):
    op: ClassVar[Literal["GrantCapability"]] = "GrantCapability"
    grant: CapabilityGrant


@dataclass(frozen=True, slots=True)
class OperatorRevokeCapability(OperatorOperationRequest[RevokeCapabilityOutcome]):
    op: ClassVar[Literal["RevokeCapability"]] = "RevokeCapability"
    capability_id: str


@dataclass(frozen=True, slots=True)
class ResolveQuarantinedDispute(OperatorOperationRequest[ResolveDisputeOutcome]):
    op: ClassVar[Literal["ResolveQuarantinedDispute"]] = "ResolveQuarantinedDispute"
    attempt_id: int
    resolution: Literal["Confirmed", "NotApplied", "Aborted"]
    evidence_region_digest: str | None
    operator_id: str


@dataclass(frozen=True, slots=True)
class OperatorPersistFence(OperatorOperationRequest[PersistFenceOutcome]):
    op: ClassVar[Literal["PersistFence"]] = "PersistFence"
    generation: int


@dataclass(frozen=True, slots=True)
class GetProjectionSummary(OperatorOperationRequest[ProjectionSummary]):
    op: ClassVar[Literal["GetProjectionSummary"]] = "GetProjectionSummary"


@dataclass(frozen=True, slots=True)
class InspectJournal(OperatorOperationRequest[JournalInspection]):
    op: ClassVar[Literal["InspectJournal"]] = "InspectJournal"


@dataclass(frozen=True, slots=True)
class SubmitDecision(OperatorOperationRequest[SubmitDecisionOutcome]):
    op: ClassVar[Literal["SubmitDecision"]] = "SubmitDecision"
    attempt_id: int
    decision: str
    operator_id: str
