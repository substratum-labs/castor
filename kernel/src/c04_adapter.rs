//! C-04 Effect Adapter Contract Types and Interfaces (L5 Target)

use crate::c01_storage::PersistedEntryProof;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DispatchCommand {
    pub agent_id: String,
    pub action_id: String,
    pub attempt_id: u64,
    pub action_digest: String,
    pub request_digest: String,
    pub adapter_id: String,
    pub assurance_profile: String,
    pub attempt_armed_proof: PersistedEntryProof,
    pub dispatch_proof: PersistedEntryProof,
    pub authority_binding_digest: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReservationState {
    Reserved,
    SubmissionAttempted,
    Ambiguous,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExternalKnowledge {
    NotApplicable,
    Observed,
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdapterDedupRecord {
    pub dedup_key: (String, String, u64, String, String),
    pub dispatch_entry_digest: String,
    pub reservation_state: ReservationState,
    pub submission_observation: Option<String>,
    pub external_knowledge: ExternalKnowledge,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DeliverOutcome {
    SubmissionObserved {
        accepted_and_durable: bool,
        external_knowledge: ExternalKnowledge,
    },
    DuplicateDelivery {
        accepted_and_durable: bool,
        prior_external_knowledge: ExternalKnowledge,
    },
    NotSubmittedProven,
    Ambiguous {
        accepted_and_durable: bool,
    },
    RejectedInvalidCommand(String),
    UnavailableBeforeReservation,
}

pub trait EffectAdapter {
    fn deliver_armed_attempt(&mut self, command: DispatchCommand) -> DeliverOutcome;
    fn report_effect_observation(&mut self, attempt_id: u64, observation_digest: &str) -> bool;
}

/// Pre-implementation stub for EffectAdapter (fails until Phase 3 implementation)
#[derive(Default)]
pub struct PreImplementationEffectAdapter;

impl PreImplementationEffectAdapter {
    pub fn new() -> Self {
        Self
    }
}

impl EffectAdapter for PreImplementationEffectAdapter {
    fn deliver_armed_attempt(&mut self, _command: DispatchCommand) -> DeliverOutcome {
        DeliverOutcome::UnavailableBeforeReservation
    }

    fn report_effect_observation(&mut self, _attempt_id: u64, _observation_digest: &str) -> bool {
        false
    }
}
