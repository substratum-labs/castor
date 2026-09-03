//! C-05 admission, evidence, and settlement contract vocabulary.
//!
//! Phase 2 intentionally exposes only the test-facing boundary.  The D1
//! implementation belongs to Phase 3; until then every mutating operation
//! fails closed with `RejectedPreimplementation`.

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmissionCertificate {
    pub action_id: String,
    pub action_digest: String,
    pub generation: u64,
    pub target_scope: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmissionRequest {
    pub agent_id: String,
    pub action_id: String,
    pub action_digest: String,
    pub target_scope: String,
    pub capability_id: String,
    pub entry_id: u64,
    pub certificate: AdmissionCertificate,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EvidenceBundle {
    pub region_id: String,
    pub digest: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResolutionClass {
    Confirmed,
    NotApplied,
    Unknown,
    PartiallyApplied,
    Divergent,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProofClass {
    VerifiableNonExecution,
    ProviderConfirmation,
    TimeoutTelemetry,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SettlementCertificate {
    pub certificate_id: String,
    pub attempt_id: u64,
    pub dispatch_identity: String,
    pub evidence_bundle_digest: String,
    pub proposed_resolution: ResolutionClass,
    pub proof_class: ProofClass,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SettlementRequest {
    pub agent_id: String,
    pub evidence_region_id: String,
    pub entry_id: u64,
    pub certificate: SettlementCertificate,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AttemptStatus {
    ArmedUnknown,
    Dispatched,
    Settled,
    QuarantinedDispute,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SettlementOutcome {
    AttemptArmedAck {
        attempt_id: u64,
        entry_id: u64,
    },
    DispatchRecordedAck {
        entry_id: u64,
    },
    EvidencePersisted,
    AcceptedAndAppended {
        entry_id: u64,
        resolution: ResolutionClass,
    },
    AlreadyAppendedSameCertificate {
        entry_id: u64,
    },
    ConflictingEvidenceAppended,
    RejectedCurrentState,
    RejectedStaleGeneration {
        current_generation: u64,
    },
    RejectedCapabilityRevoked,
    RejectedInvalidProofClass,
    IntegrityOrProtocolFault,
    RejectedPreimplementation,
}

/// Test-facing contract boundary; no transport, storage schema, or wire
/// certificate format is frozen by this declaration.
pub trait AdmissionSettlement {
    fn present_admission_certificate(&mut self, request: AdmissionRequest) -> SettlementOutcome;
    fn record_dispatch_attempt(
        &mut self,
        attempt_id: u64,
        dispatch_identity: &str,
        entry_id: u64,
    ) -> SettlementOutcome;
    fn persist_evidence(&mut self, evidence: EvidenceBundle) -> SettlementOutcome;
    fn present_settlement_certificate(&mut self, request: SettlementRequest) -> SettlementOutcome;
    fn reconstruct_after_crash(&mut self);
    fn attempt_status(&self, action_id: &str) -> Option<AttemptStatus>;
    fn fence_generation(&mut self, new_generation: u64);
    fn revoke_capability(&mut self, capability_id: &str);
    fn set_roche_isolation_unknown(&mut self);
}

/// Deliberately fail-closed Phase-2 placeholder for the Profile-D1 boundary.
#[derive(Default)]
pub struct D1Settlement;

impl D1Settlement {
    pub fn for_test(_agent_id: &str, _generation: u64) -> Self {
        Self
    }
}

impl AdmissionSettlement for D1Settlement {
    fn present_admission_certificate(&mut self, _request: AdmissionRequest) -> SettlementOutcome {
        SettlementOutcome::RejectedPreimplementation
    }

    fn record_dispatch_attempt(
        &mut self,
        _attempt_id: u64,
        _dispatch_identity: &str,
        _entry_id: u64,
    ) -> SettlementOutcome {
        SettlementOutcome::RejectedPreimplementation
    }

    fn persist_evidence(&mut self, _evidence: EvidenceBundle) -> SettlementOutcome {
        SettlementOutcome::RejectedPreimplementation
    }

    fn present_settlement_certificate(&mut self, _request: SettlementRequest) -> SettlementOutcome {
        SettlementOutcome::RejectedPreimplementation
    }

    fn reconstruct_after_crash(&mut self) {}

    fn attempt_status(&self, _action_id: &str) -> Option<AttemptStatus> {
        None
    }

    fn fence_generation(&mut self, _new_generation: u64) {}

    fn revoke_capability(&mut self, _capability_id: &str) {}

    fn set_roche_isolation_unknown(&mut self) {}
}
