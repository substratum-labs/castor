//! C-05 admission, evidence, and settlement contract vocabulary.
//!
//! The D1 implementation is an in-memory reference state machine for the
//! contract boundary. It deliberately keeps transport and storage concerns
//! outside this module.

use std::collections::{HashMap, HashSet};

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

#[derive(Clone, Debug)]
struct Attempt {
    action_digest: String,
    target_scope: String,
    admission_entry_id: u64,
    status: AttemptStatus,
    dispatch_identity: Option<String>,
    resolution: Option<ResolutionClass>,
}

/// In-memory Profile-D1 implementation of the admission and settlement
/// contract. `for_test` supplies its authority context explicitly.
pub struct D1Settlement {
    agent_id: String,
    generation: u64,
    next_attempt_id: u64,
    attempts: HashMap<u64, Attempt>,
    attempt_by_action: HashMap<String, u64>,
    persisted_evidence: HashMap<String, String>,
    certificate_entries: HashMap<String, u64>,
    revoked_capabilities: HashSet<String>,
    roche_isolation_unknown: bool,
}

impl D1Settlement {
    pub fn for_test(agent_id: &str, generation: u64) -> Self {
        Self {
            agent_id: agent_id.into(),
            generation,
            next_attempt_id: 1,
            attempts: HashMap::new(),
            attempt_by_action: HashMap::new(),
            persisted_evidence: HashMap::new(),
            certificate_entries: HashMap::new(),
            revoked_capabilities: HashSet::new(),
            roche_isolation_unknown: false,
        }
    }

    fn has_active_attempt(&self) -> bool {
        self.attempts.values().any(|attempt| {
            matches!(
                attempt.status,
                AttemptStatus::ArmedUnknown | AttemptStatus::Dispatched
            )
        })
    }
}

impl AdmissionSettlement for D1Settlement {
    fn present_admission_certificate(&mut self, request: AdmissionRequest) -> SettlementOutcome {
        if request.agent_id != self.agent_id
            || request.certificate.action_id != request.action_id
            || request.certificate.action_digest != request.action_digest
            || request.certificate.target_scope != request.target_scope
        {
            return SettlementOutcome::IntegrityOrProtocolFault;
        }
        if request.certificate.generation != self.generation {
            return SettlementOutcome::RejectedStaleGeneration {
                current_generation: self.generation,
            };
        }
        if self.revoked_capabilities.contains(&request.capability_id) {
            return SettlementOutcome::RejectedCapabilityRevoked;
        }
        if let Some(&attempt_id) = self.attempt_by_action.get(&request.action_id) {
            let attempt = &self.attempts[&attempt_id];
            if attempt.action_digest == request.action_digest
                && attempt.target_scope == request.target_scope
                && attempt.admission_entry_id == request.entry_id
            {
                return SettlementOutcome::AttemptArmedAck {
                    attempt_id,
                    entry_id: request.entry_id,
                };
            }
            return SettlementOutcome::RejectedCurrentState;
        }
        if self.roche_isolation_unknown || self.has_active_attempt() {
            return SettlementOutcome::RejectedCurrentState;
        }

        let attempt_id = self.next_attempt_id;
        self.next_attempt_id += 1;
        self.attempt_by_action
            .insert(request.action_id.clone(), attempt_id);
        self.attempts.insert(
            attempt_id,
            Attempt {
                action_digest: request.action_digest,
                target_scope: request.target_scope,
                admission_entry_id: request.entry_id,
                status: AttemptStatus::ArmedUnknown,
                dispatch_identity: None,
                resolution: None,
            },
        );
        SettlementOutcome::AttemptArmedAck {
            attempt_id,
            entry_id: request.entry_id,
        }
    }

    fn record_dispatch_attempt(
        &mut self,
        attempt_id: u64,
        dispatch_identity: &str,
        entry_id: u64,
    ) -> SettlementOutcome {
        if dispatch_identity.is_empty() {
            return SettlementOutcome::IntegrityOrProtocolFault;
        }
        let Some(attempt) = self.attempts.get_mut(&attempt_id) else {
            return SettlementOutcome::RejectedCurrentState;
        };
        match &attempt.dispatch_identity {
            Some(identity) if identity == dispatch_identity => {
                SettlementOutcome::DispatchRecordedAck { entry_id }
            }
            Some(_) => SettlementOutcome::IntegrityOrProtocolFault,
            None if attempt.status == AttemptStatus::ArmedUnknown => {
                attempt.dispatch_identity = Some(dispatch_identity.into());
                attempt.status = AttemptStatus::Dispatched;
                SettlementOutcome::DispatchRecordedAck { entry_id }
            }
            None => SettlementOutcome::RejectedCurrentState,
        }
    }

    fn persist_evidence(&mut self, evidence: EvidenceBundle) -> SettlementOutcome {
        if evidence.region_id.is_empty() || evidence.digest.is_empty() {
            return SettlementOutcome::IntegrityOrProtocolFault;
        }
        self.persisted_evidence
            .insert(evidence.region_id, evidence.digest);
        SettlementOutcome::EvidencePersisted
    }

    fn present_settlement_certificate(&mut self, request: SettlementRequest) -> SettlementOutcome {
        if request.agent_id != self.agent_id {
            return SettlementOutcome::IntegrityOrProtocolFault;
        }
        if let Some(&entry_id) = self
            .certificate_entries
            .get(&request.certificate.certificate_id)
        {
            return SettlementOutcome::AlreadyAppendedSameCertificate { entry_id };
        }
        if self.persisted_evidence.get(&request.evidence_region_id)
            != Some(&request.certificate.evidence_bundle_digest)
        {
            return SettlementOutcome::IntegrityOrProtocolFault;
        }
        let Some(attempt) = self.attempts.get_mut(&request.certificate.attempt_id) else {
            return SettlementOutcome::RejectedCurrentState;
        };
        if !matches!(
            (
                request.certificate.proposed_resolution,
                request.certificate.proof_class
            ),
            (ResolutionClass::Confirmed, ProofClass::ProviderConfirmation)
                | (
                    ResolutionClass::NotApplied,
                    ProofClass::VerifiableNonExecution
                )
        ) {
            return SettlementOutcome::RejectedInvalidProofClass;
        }
        if attempt.dispatch_identity.as_deref()
            != Some(request.certificate.dispatch_identity.as_str())
        {
            return SettlementOutcome::RejectedCurrentState;
        }
        if attempt.status == AttemptStatus::QuarantinedDispute {
            return SettlementOutcome::RejectedCurrentState;
        }
        if attempt.status == AttemptStatus::Settled {
            self.certificate_entries
                .insert(request.certificate.certificate_id, request.entry_id);
            attempt.status = AttemptStatus::QuarantinedDispute;
            return SettlementOutcome::ConflictingEvidenceAppended;
        }
        if attempt.status != AttemptStatus::Dispatched {
            return SettlementOutcome::RejectedCurrentState;
        }

        attempt.status = AttemptStatus::Settled;
        attempt.resolution = Some(request.certificate.proposed_resolution);
        self.certificate_entries
            .insert(request.certificate.certificate_id, request.entry_id);
        SettlementOutcome::AcceptedAndAppended {
            entry_id: request.entry_id,
            resolution: request.certificate.proposed_resolution,
        }
    }

    fn reconstruct_after_crash(&mut self) {
        for attempt in self.attempts.values_mut() {
            if attempt.status == AttemptStatus::ArmedUnknown {
                attempt.dispatch_identity = None;
            }
        }
    }

    fn attempt_status(&self, action_id: &str) -> Option<AttemptStatus> {
        self.attempt_by_action
            .get(action_id)
            .and_then(|attempt_id| self.attempts.get(attempt_id))
            .map(|attempt| attempt.status)
    }

    fn fence_generation(&mut self, new_generation: u64) {
        self.generation = self.generation.max(new_generation);
    }

    fn revoke_capability(&mut self, capability_id: &str) {
        self.revoked_capabilities.insert(capability_id.into());
    }

    fn set_roche_isolation_unknown(&mut self) {
        self.roche_isolation_unknown = true;
    }
}
