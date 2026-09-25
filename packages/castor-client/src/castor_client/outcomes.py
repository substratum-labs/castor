"""D1 outcome shapes returned by castord.

These are static wire descriptions. AISA framing and response shape are checked
by the client; Rust remains responsible for the meaning of each outcome.
"""

from __future__ import annotations

from typing import Literal, TypeAlias, TypedDict


class UnsettledEffect(TypedDict):
    attempt_id: int
    action_id: str
    target_scope: str
    status: str
    stable_op_id: str | None
    lock_state: str
    ambiguous_delivery: bool


class UnsettledEffectsSnapshot(TypedDict):
    author: str
    turn_id: int
    region_ref: str
    attempts: list[UnsettledEffect]


class ConsumedInteractionPayload(TypedDict):
    interaction_id: str
    observation_region_id: str
    observation_digest: str
    content: list[int]
    lease_epoch: int


class RejectedOutcome(TypedDict):
    type: Literal[
        "Ambiguous",
        "RejectedStaleAuthority",
        "RejectedCapabilityRevoked",
        "RejectedInvalidProofClass",
        "RejectedBindingOrIssuer",
        "RejectedLateOrClosedTurn",
        "RejectedCurrentState",
        "RejectedNotFound",
        "RejectedPrecondition",
        "IntegrityOrProtocolFault",
        "UnavailableBeforeAck",
    ]


class RejectedStaleGeneration(TypedDict):
    type: Literal["RejectedStaleGeneration"]
    current_generation: int


FailureOutcome: TypeAlias = RejectedOutcome | RejectedStaleGeneration


class Admitted(TypedDict):
    type: Literal["Admitted"]
    unsettled_effects_snapshot: UnsettledEffectsSnapshot


class InteractionRequested(TypedDict):
    type: Literal["InteractionRequested"]


class InteractionBound(TypedDict):
    type: Literal["InteractionBound"]


class InteractionConsumed(TypedDict):
    type: Literal["InteractionConsumed"]
    payload: ConsumedInteractionPayload


class TurnCommitted(TypedDict):
    type: Literal["TurnCommitted"]


class ActionRegistered(TypedDict):
    type: Literal["ActionRegistered"]


class AttemptArmed(TypedDict):
    type: Literal["AttemptArmed"]
    attempt_id: int


class DispatchRecorded(TypedDict):
    type: Literal["DispatchRecorded"]


class GenerationFenced(TypedDict):
    type: Literal["GenerationFenced"]
    generation: int


class CapabilityGranted(TypedDict):
    type: Literal["CapabilityGranted"]


class CapabilityRevoked(TypedDict):
    type: Literal["CapabilityRevoked"]


class EntryPersisted(TypedDict):
    type: Literal["EntryPersisted"]


class DecisionSubmitted(TypedDict):
    type: Literal["DecisionSubmitted"]


class RegionOutcome(TypedDict):
    type: Literal[
        "Success",
        "AlreadyPersistedSameContent",
        "RejectedIdentityConflict",
        "UnavailableBeforeAck",
        "IntegrityFault",
    ]


class RecoverySummary(TypedDict):
    phase: str
    probe_budget_remaining: int


class ProjectionSummary(TypedDict):
    recovery: RecoverySummary
    generation: int
    active_capabilities: int
    locked_scopes: int
    quarantined_disputes: int
    journal_entries: int


class JournalInspection(TypedDict):
    entries: list[dict[str, object]]


AdmitTurnOutcome: TypeAlias = Admitted | FailureOutcome
RequestInteractionOutcome: TypeAlias = InteractionRequested | FailureOutcome
ReportOutcomeResult: TypeAlias = InteractionBound | FailureOutcome
ConsumeInteractionOutcome: TypeAlias = InteractionConsumed | FailureOutcome
CommitTurnOutcome: TypeAlias = TurnCommitted | FailureOutcome
RegisterActionOutcome: TypeAlias = ActionRegistered | FailureOutcome
PresentAdmissionOutcome: TypeAlias = AttemptArmed | FailureOutcome
RecordDispatchOutcome: TypeAlias = DispatchRecorded | FailureOutcome
PersistFenceOutcome: TypeAlias = GenerationFenced | FailureOutcome
RevokeCapabilityOutcome: TypeAlias = CapabilityRevoked | FailureOutcome
GrantCapabilityOutcome: TypeAlias = CapabilityGranted | FailureOutcome
ResolveDisputeOutcome: TypeAlias = EntryPersisted | FailureOutcome
SubmitDecisionOutcome: TypeAlias = DecisionSubmitted | FailureOutcome
