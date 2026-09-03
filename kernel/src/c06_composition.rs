//! C-06 governed-turn composition boundary.
//!
//! Phase 2 freezes the single-owner authority shape and operation vocabulary.
//! It intentionally does not project any lifecycle state yet: every operation
//! fails closed, so no caller can accidentally cross an unimplemented seam.

use crate::c01_storage::D1DurableStorage;
use std::io;
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GovernedTurnOutcome {
    RejectedPreimplementation,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmitTurnRequest {
    pub agent_id: String,
    pub turn_id: u64,
    pub lease_epoch: u64,
    pub base_projection_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestInteractionRequest {
    pub interaction_id: String,
    pub lease_epoch: u64,
    pub request_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InteractionOutcomeReport {
    pub interaction_id: String,
    pub observation_region_id: String,
    pub observation_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConsumeInteractionRequest {
    pub interaction_id: String,
    pub lease_epoch: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommitTurnRequest {
    pub lease_epoch: u64,
    pub base_projection_digest: String,
    pub successor_region_id: String,
    pub successor_digest: String,
    pub action_manifest: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionRegistrationRequest {
    pub action_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PresentAdmissionCertificateRequest {
    pub action_id: String,
    pub target_scope: String,
    pub capability_id: String,
    pub generation: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecordDispatchAttemptRequest {
    pub attempt_id: u64,
    pub dispatch_identity: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeliverArmedAttemptRequest {
    pub attempt_id: u64,
    pub dispatch_identity: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PresentSettlementCertificateRequest {
    pub attempt_id: u64,
    pub dispatch_identity: String,
    pub evidence_region_id: String,
    pub evidence_digest: String,
    pub resolution: String,
    pub proof_class: String,
}

/// The sole composition authority. The embedded C-01 store is deliberately
/// private so all future lifecycle transitions retain one semantic writer.
pub struct D1GovernedTurnAuthority {
    storage: D1DurableStorage,
}

impl D1GovernedTurnAuthority {
    pub fn open(path: impl AsRef<Path>) -> io::Result<Self> {
        D1DurableStorage::open(path).map(Self::for_test)
    }

    /// Test-facing constructor for a caller that already owns the sole C-01
    /// storage handle. Production callers should use [`Self::open`].
    pub fn for_test(storage: D1DurableStorage) -> Self {
        Self { storage }
    }

    /// Read-only inspection preserves the one-writer boundary.
    pub fn storage(&self) -> &D1DurableStorage {
        &self.storage
    }

    pub fn admit_turn(&mut self, _request: AdmitTurnRequest) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn request_interaction(
        &mut self,
        _request: RequestInteractionRequest,
    ) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn report_outcome(&mut self, _report: InteractionOutcomeReport) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn consume_interaction(
        &mut self,
        _request: ConsumeInteractionRequest,
    ) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn commit_turn(&mut self, _request: CommitTurnRequest) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn register_action(&mut self, _request: ActionRegistrationRequest) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn present_admission_certificate(
        &mut self,
        _request: PresentAdmissionCertificateRequest,
    ) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn record_dispatch_attempt(
        &mut self,
        _request: RecordDispatchAttemptRequest,
    ) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn deliver_armed_attempt(
        &mut self,
        _request: DeliverArmedAttemptRequest,
    ) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn present_settlement_certificate(
        &mut self,
        _request: PresentSettlementCertificateRequest,
    ) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn persist_fence(&mut self, _generation: u64) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn revoke_capability(&mut self, _capability_id: &str) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    pub fn reconstruct_after_crash(&mut self) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }
}
