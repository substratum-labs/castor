------------------------ MODULE CognitiveRecovery ------------------------
EXTENDS Naturals, FiniteSets, Sequences, TLC

(*
  Formal TLA+ specification for Castor Knowledge-Driven Cognitive Recovery
  EPIC-34 / Phase A (T-314-A).

  Models the complete two-level control hierarchy and adversarial races:
  1. Actuator physical ground truth (Unexecuted, Committed, TerminatedRejected)
  2. Network transport in-flight packet decoupling (Late arrivals, drops)
  3. Microkernel attempt state machine (ArmedUnknown, Dispatched, Confirmed, NotApplied, QuarantinedDispute)
  4. Node-local EvidenceService TCB boundary (Guest material vs Authenticated Settlement)
  5. Cross-Turn recovery lifecycle (Reveal -> Probe -> Settle -> Resume/Replan/Escalate)
  6. Durable journal reconstruction across daemon restarts (Budget preservation)
*)

CONSTANTS 
    TurnIds,       \* e.g., 1..2
    Actions,       \* e.g., {A1, A2}
    MaxProbeBudget \* e.g., 2

Scopes == {"payment-scope"}

ExternalStates == {"Unexecuted", "Committed", "TerminatedRejected"}
KernelAttemptStates == {"None", "ArmedUnknown", "Dispatched", "Confirmed", "NotApplied", "QuarantinedDispute"}
RecoveryPhase == {"Idle", "NeedsEvidence", "Probing", "Verifying", "Resolved", "Escalated"}

VARIABLES 
    currentTurn,
    externalActuatorState,  \* [a \in Actions |-> ExternalStates]
    networkPacket,          \* [a \in Actions |-> BOOLEAN] (decoupled packet in flight)
    attemptState,           \* [a \in Actions |-> KernelAttemptStates]
    scopeLocked,            \* [s \in Scopes |-> BOOLEAN]
    recoveryPhase,          \* RecoveryPhase
    candidateEvidence,      \* [a \in Actions |-> BOOLEAN]
    authenticatedEvidence,  \* [a \in Actions |-> {"None", "Confirmed", "NotApplied", "Conflicting"}]
    probeBudgetRemaining,   \* 0..MaxProbeBudget (RAM)
    journaledBudget,        \* 0..MaxProbeBudget (durable on disk)
    journaledEscalated,     \* BOOLEAN (durable on disk)
    capabilityActive,       \* BOOLEAN (C-06 capability grant active)
    committedCount,         \* [a \in Actions |-> 0..1]
    lastSettlementDigest    \* [a \in Actions |-> 0..2]

vars == <<currentTurn, externalActuatorState, networkPacket, attemptState, scopeLocked,
          recoveryPhase, candidateEvidence, authenticatedEvidence,
          probeBudgetRemaining, journaledBudget, journaledEscalated,
          capabilityActive, committedCount, lastSettlementDigest>>

TypeOK ==
    /\ currentTurn \in TurnIds
    /\ externalActuatorState \in [Actions -> ExternalStates]
    /\ networkPacket \in [Actions -> BOOLEAN]
    /\ attemptState \in [Actions -> KernelAttemptStates]
    /\ scopeLocked \in [Scopes -> BOOLEAN]
    /\ recoveryPhase \in RecoveryPhase
    /\ candidateEvidence \in [Actions -> BOOLEAN]
    /\ authenticatedEvidence \in [Actions -> {"None", "Confirmed", "NotApplied", "Conflicting"}]
    /\ probeBudgetRemaining \in 0..MaxProbeBudget
    /\ journaledBudget \in 0..MaxProbeBudget
    /\ journaledEscalated \in BOOLEAN
    /\ capabilityActive \in BOOLEAN
    /\ committedCount \in [Actions -> 0..1]
    /\ lastSettlementDigest \in [Actions -> 0..2]

Init ==
    /\ currentTurn = 1
    /\ externalActuatorState = [a \in Actions |-> "Unexecuted"]
    /\ networkPacket = [a \in Actions |-> FALSE]
    /\ attemptState = [a \in Actions |-> "None"]
    /\ scopeLocked = [s \in Scopes |-> FALSE]
    /\ recoveryPhase = "Idle"
    /\ candidateEvidence = [a \in Actions |-> FALSE]
    /\ authenticatedEvidence = [a \in Actions |-> "None"]
    /\ probeBudgetRemaining = MaxProbeBudget
    /\ journaledBudget = MaxProbeBudget
    /\ journaledEscalated = FALSE
    /\ capabilityActive = TRUE
    /\ committedCount = [a \in Actions |-> 0]
    /\ lastSettlementDigest = [a \in Actions |-> 0]

-----------------------------------------------------------------------------
(* Helper Predicates *)

ActiveConflictInScope ==
    \E a \in Actions : attemptState[a] \in {"ArmedUnknown", "Dispatched", "QuarantinedDispute"}

-----------------------------------------------------------------------------
(* Core Execution & Dispatch Seam *)

(* PresentAdmission: Core checks active capability and verifies scope is not locked *)
PresentAdmission(a) ==
    /\ capabilityActive
    /\ attemptState[a] = "None"
    /\ ~scopeLocked["payment-scope"]
    /\ attemptState' = [attemptState EXCEPT ![a] = "ArmedUnknown"]
    /\ scopeLocked' = [scopeLocked EXCEPT !["payment-scope"] = TRUE]
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, recoveryPhase,
                   candidateEvidence, authenticatedEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* RecordDispatch: Decoupled! Changes kernel attempt to Dispatched and puts packet on the network *)
(* Actuator state is NOT mutated by kernel dispatch; remains Unexecuted or TerminatedRejected *)
RecordDispatch(a) ==
    /\ attemptState[a] = "ArmedUnknown"
    /\ attemptState' = [attemptState EXCEPT ![a] = "Dispatched"]
    /\ networkPacket' = [networkPacket EXCEPT ![a] = TRUE]
    /\ UNCHANGED <<currentTurn, externalActuatorState, scopeLocked, recoveryPhase,
                   candidateEvidence, authenticatedEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Actuator receives in-flight packet and executes physical effect *)
ActuatorReceiveAndExecute(a) ==
    /\ networkPacket[a]
    /\ externalActuatorState[a] = "Unexecuted"
    /\ externalActuatorState' = [externalActuatorState EXCEPT ![a] = "Committed"]
    /\ committedCount' = [committedCount EXCEPT ![a] = committedCount[a] + 1]
    /\ networkPacket' = [networkPacket EXCEPT ![a] = FALSE]
    /\ UNCHANGED <<currentTurn, attemptState, scopeLocked, recoveryPhase,
                   candidateEvidence, authenticatedEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive,
                   lastSettlementDigest>>

(* Actuator Atomically Terminates & Tombstones operation under stable op ID *)
(* Class A resolution: Adapter-authorized recovery action for existing Attempt *)
ActuatorTerminateAndReject(a) ==
    /\ externalActuatorState[a] = "Unexecuted"
    /\ externalActuatorState' = [externalActuatorState EXCEPT ![a] = "TerminatedRejected"]
    /\ UNCHANGED <<currentTurn, networkPacket, attemptState, scopeLocked, recoveryPhase,
                   candidateEvidence, authenticatedEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Late Arrival at Tombstone: network packet arrives after terminate-and-reject *)
(* Physical effect is hard-rejected; committedCount is NOT incremented; tombstone is sticky *)
ActuatorLateArrivalAtTombstone(a) ==
    /\ networkPacket[a]
    /\ externalActuatorState[a] = "TerminatedRejected"
    /\ networkPacket' = [networkPacket EXCEPT ![a] = FALSE]
    /\ UNCHANGED <<currentTurn, externalActuatorState, attemptState, scopeLocked,
                   recoveryPhase, candidateEvidence, authenticatedEvidence,
                   probeBudgetRemaining, journaledBudget, journaledEscalated,
                   capabilityActive, committedCount, lastSettlementDigest>>

-----------------------------------------------------------------------------
(* Failure & Turn Boundary Seam *)

(* Worker Crash: advances turn, synthesizes authoritative snapshot into observation *)
WorkerCrashAndAdvanceTurn ==
    /\ currentTurn < 2
    /\ currentTurn' = currentTurn + 1
    /\ recoveryPhase' = IF ActiveConflictInScope THEN "NeedsEvidence" ELSE "Idle"
    /\ candidateEvidence' = [a \in Actions |-> FALSE]
    /\ UNCHANGED <<externalActuatorState, networkPacket, attemptState, scopeLocked,
                   authenticatedEvidence, probeBudgetRemaining, journaledBudget,
                   journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Revoke Capability: control plane revokes capability (C.10: in-flight attempt not unarmed) *)
RevokeCapability ==
    /\ capabilityActive
    /\ capabilityActive' = FALSE
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, recoveryPhase, candidateEvidence, authenticatedEvidence,
                   probeBudgetRemaining, journaledBudget, journaledEscalated,
                   committedCount, lastSettlementDigest>>

(* Stale Snapshot Admission Attempt: replaying stale snapshot under revoked cap is REJECTED *)
StaleSnapshotAdmissionAttempt(a) ==
    /\ ~capabilityActive
    /\ attemptState[a] = "None"
    /\ UNCHANGED vars  \* RejectedPrecondition / RejectedCapabilityRevoked

-----------------------------------------------------------------------------
(* Cognitive Recovery Interaction Seam (Probe & Reconcile) *)

(* Agent issues bounded read-only query-operation Interaction *)
IssueControlledProbe(a) ==
    /\ recoveryPhase \in {"NeedsEvidence", "Probing"}
    /\ attemptState[a] \in {"ArmedUnknown", "Dispatched"}
    /\ probeBudgetRemaining > 0
    /\ probeBudgetRemaining' = probeBudgetRemaining - 1
    /\ journaledBudget' = probeBudgetRemaining - 1
    /\ recoveryPhase' = "Probing"
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, candidateEvidence, authenticatedEvidence,
                   journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Actuator responds with receipt: creates candidate evidence *)
ActuatorProbeResponse(a) ==
    /\ recoveryPhase = "Probing"
    /\ externalActuatorState[a] \in {"Committed", "TerminatedRejected"}
    /\ candidateEvidence' = [candidateEvidence EXCEPT ![a] = TRUE]
    /\ recoveryPhase' = "Verifying"
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, authenticatedEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Query returns not_found: preserve Unknown! Does not mint NotApplied *)
ActuatorProbeNotFound(a) ==
    /\ recoveryPhase = "Probing"
    /\ externalActuatorState[a] = "Unexecuted"
    /\ candidateEvidence' = [candidateEvidence EXCEPT ![a] = FALSE]
    /\ recoveryPhase' = IF probeBudgetRemaining = 0 THEN "Escalated" ELSE "NeedsEvidence"
    /\ journaledEscalated' = (probeBudgetRemaining = 0)
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, authenticatedEvidence, probeBudgetRemaining,
                   journaledBudget, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* EvidenceService TCB Barrier: verifies candidate material and mints authenticated proof *)
EvidenceServiceVerifyAndMint(a) ==
    /\ recoveryPhase \in {"Verifying", "Escalated"}
    /\ candidateEvidence[a]
    /\ IF externalActuatorState[a] = "Committed"
       THEN authenticatedEvidence' = [authenticatedEvidence EXCEPT ![a] = "Confirmed"]
       ELSE IF externalActuatorState[a] = "TerminatedRejected"
            THEN authenticatedEvidence' = [authenticatedEvidence EXCEPT ![a] = "NotApplied"]
            ELSE authenticatedEvidence' = authenticatedEvidence
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, recoveryPhase, candidateEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* EvidenceService detects conflicting external receipts -> mints Conflicting *)
EvidenceServiceDetectConflict(a) ==
    /\ recoveryPhase \in {"Verifying", "Escalated"}
    /\ candidateEvidence[a]
    /\ authenticatedEvidence' = [authenticatedEvidence EXCEPT ![a] = "Conflicting"]
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, recoveryPhase, candidateEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Kernel Commit Settlement: Core verifies EvidenceService signature and transitions attempt *)
KernelCommitSettlement(a) ==
    /\ authenticatedEvidence[a] \in {"Confirmed", "NotApplied"}
    /\ attemptState[a] \in {"ArmedUnknown", "Dispatched"}
    /\ attemptState' = [attemptState EXCEPT ![a] = authenticatedEvidence[a]]
    /\ lastSettlementDigest' = [lastSettlementDigest EXCEPT ![a] = 1]
    /\ scopeLocked' = [scopeLocked EXCEPT !["payment-scope"] = 
                        \E b \in Actions \ {a} : attemptState[b] \in {"ArmedUnknown", "Dispatched", "QuarantinedDispute"}]
    /\ recoveryPhase' = "Resolved"
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, candidateEvidence,
                   authenticatedEvidence, probeBudgetRemaining, journaledBudget,
                   journaledEscalated, capabilityActive, committedCount>>

(* Kernel Conflicting Settlement: enters QuarantinedDispute *)
KernelCommitDispute(a) ==
    /\ authenticatedEvidence[a] = "Conflicting"
    /\ attemptState[a] \in {"ArmedUnknown", "Dispatched"}
    /\ attemptState' = [attemptState EXCEPT ![a] = "QuarantinedDispute"]
    /\ recoveryPhase' = "Escalated"
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, scopeLocked,
                   candidateEvidence, authenticatedEvidence, probeBudgetRemaining,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Operator resolves QuarantinedDispute via control plane SubmitDecision *)
OperatorSubmitDecision(a, res) ==
    /\ res \in {"Confirmed", "NotApplied"}
    /\ attemptState[a] = "QuarantinedDispute"
    /\ (res = "NotApplied" => (externalActuatorState[a] = "TerminatedRejected" /\ committedCount[a] = 0))
    /\ (res = "Confirmed" => (externalActuatorState[a] = "Committed"))
    /\ attemptState' = [attemptState EXCEPT ![a] = res]
    /\ scopeLocked' = [scopeLocked EXCEPT !["payment-scope"] = 
                        \E b \in Actions \ {a} : attemptState[b] \in {"ArmedUnknown", "Dispatched", "QuarantinedDispute"}]
    /\ recoveryPhase' = "Resolved"
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, candidateEvidence,
                   authenticatedEvidence, probeBudgetRemaining, journaledBudget,
                   journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

(* Settlement Idempotence (RFC S5): duplicate presentation of same valid settlement *)
IdempotentSettlementReplay(a) ==
    /\ authenticatedEvidence[a] \in {"Confirmed", "NotApplied"}
    /\ attemptState[a] = authenticatedEvidence[a]
    /\ lastSettlementDigest[a] = 1
    /\ UNCHANGED vars  \* Idempotent ACK, journal unchanged, projection unchanged

(* Guest Forge Attempt: Ring-3 trying to mint settlement directly without EvidenceService *)
GuestForgeSettlementAttempt(a) ==
    /\ authenticatedEvidence[a] = "None"
    /\ UNCHANGED vars  \* UnauthorizedOpcode; state completely unaffected

(* Budget Depleted Escalation (R10) *)
BudgetDepletedEscalate ==
    /\ recoveryPhase \in {"NeedsEvidence", "Probing"}
    /\ probeBudgetRemaining = 0
    /\ recoveryPhase' = "Escalated"
    /\ journaledEscalated' = TRUE
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, candidateEvidence, authenticatedEvidence,
                   probeBudgetRemaining, journaledBudget, capabilityActive,
                   committedCount, lastSettlementDigest>>

(* Daemon Restart (T5 / S6): crash and reconstruct from journal *)
RestartDaemon ==
    /\ probeBudgetRemaining' = journaledBudget  \* Preserves consumed quota from journal!
    /\ recoveryPhase' = IF journaledEscalated THEN "Escalated" ELSE recoveryPhase
    /\ UNCHANGED <<currentTurn, externalActuatorState, networkPacket, attemptState,
                   scopeLocked, candidateEvidence, authenticatedEvidence,
                   journaledBudget, journaledEscalated, capabilityActive, committedCount,
                   lastSettlementDigest>>

-----------------------------------------------------------------------------
(* Next State Specification *)

Next ==
    \/ \E a \in Actions : PresentAdmission(a)
    \/ \E a \in Actions : RecordDispatch(a)
    \/ \E a \in Actions : ActuatorReceiveAndExecute(a)
    \/ \E a \in Actions : ActuatorTerminateAndReject(a)
    \/ \E a \in Actions : ActuatorLateArrivalAtTombstone(a)
    \/ WorkerCrashAndAdvanceTurn
    \/ RevokeCapability
    \/ \E a \in Actions : StaleSnapshotAdmissionAttempt(a)
    \/ \E a \in Actions : IssueControlledProbe(a)
    \/ \E a \in Actions : ActuatorProbeResponse(a)
    \/ \E a \in Actions : ActuatorProbeNotFound(a)
    \/ \E a \in Actions : EvidenceServiceVerifyAndMint(a)
    \/ \E a \in Actions : EvidenceServiceDetectConflict(a)
    \/ \E a \in Actions : KernelCommitSettlement(a)
    \/ \E a \in Actions : KernelCommitDispute(a)
    \/ \E a \in Actions : \E res \in {"Confirmed", "NotApplied"} : OperatorSubmitDecision(a, res)
    \/ \E a \in Actions : IdempotentSettlementReplay(a)
    \/ \E a \in Actions : GuestForgeSettlementAttempt(a)
    \/ BudgetDepletedEscalate
    \/ RestartDaemon

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* Safety Invariants (S1 - S7) *)

(* S1: UnauthenticatedEvidenceRejected *)
S1_UnauthenticatedEvidenceRejected ==
    \A a \in Actions :
        (attemptState[a] \in {"Confirmed", "NotApplied"} /\ lastSettlementDigest[a] > 0) =>
        (authenticatedEvidence[a] \in {"Confirmed", "NotApplied"})

(* S2: UnknownBlocksConflictingMutations *)
S2_UnknownBlocksConflictingMutations ==
    ActiveConflictInScope => scopeLocked["payment-scope"]

(* S3: ConfirmedAtMostOnce *)
S3_ConfirmedAtMostOnce ==
    \A a \in Actions :
        (attemptState[a] = "Confirmed") => (committedCount[a] <= 1)

(* S4: NotAppliedExcludesOldDispatches *)
S4_NotAppliedExcludesOldDispatches ==
    \A a \in Actions :
        (attemptState[a] = "NotApplied") => (externalActuatorState[a] = "TerminatedRejected" /\ committedCount[a] = 0)

(* S5: SettlementIdempotence *)
S5_SettlementIdempotence ==
    \A a \in Actions :
        (attemptState[a] \in {"Confirmed", "NotApplied"}) =>
        (lastSettlementDigest[a] <= 1)

(* S6: BudgetPreservedAcrossRestarts *)
S6_BudgetPreservedAcrossRestarts ==
    probeBudgetRemaining <= journaledBudget

(* S7: StaleSnapshotGrantsNoAuthority *)
S7_StaleSnapshotGrantsNoAuthority ==
    (~capabilityActive) => (\A a \in Actions : attemptState[a] /= "ArmedUnknown" \/ scopeLocked["payment-scope"])

=============================================================================
