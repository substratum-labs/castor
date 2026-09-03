//! C-03 single-node D1 observational interaction continuation.
//!
//! The interaction projection is Core-owned convenience state. C-01 region
//! persistence and conditional journal entries are the semantic boundaries;
//! no service report can bind a Region directly.

use crate::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurableStorage, EnsureRegionOutcome,
};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

static INTERACTION_STORAGE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InteractionIdentity {
    pub agent_id: String,
    pub turn_id: u64,
    pub interaction_id: String,
    pub request_digest: String,
    pub service_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InteractionRequest {
    pub identity: InteractionIdentity,
    pub lease_epoch: u64,
    pub entry_id: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InteractionResultReport {
    pub identity: InteractionIdentity,
    pub region_id: String,
    pub result_digest: String,
    pub disposition: String,
    pub entry_id: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum InteractionOutcome {
    InteractionRequestedAck {
        interaction_id: String,
        persisted_entry_id: u64,
    },
    ResultRegionStored {
        region_id: String,
        result_digest: String,
    },
    Bound {
        persisted_entry_id: u64,
        region_id: String,
    },
    AlreadyBoundSameOutcome {
        persisted_entry_id: u64,
    },
    RejectedConflictingOutcome {
        persisted_entry_id: u64,
    },
    FreshLeaseGranted {
        persisted_entry_id: u64,
        lease_epoch: u64,
    },
    InteractionConsumed {
        interaction_id: String,
        region_id: String,
    },
    TurnClosedOrFenced {
        persisted_entry_id: u64,
    },
    RejectedLateOrClosedTurn,
    RejectedStaleAuthority,
    IntegrityOrProtocolFault,
    RejectedPrecondition,
    UnavailableBeforeAck,
}

/// Test-facing semantic boundary, deliberately not a stable public API.
pub trait InteractionContinuation {
    fn request_interaction(&mut self, request: InteractionRequest) -> InteractionOutcome;

    fn persist_interaction_result_region(
        &mut self,
        agent_id: &str,
        interaction_id: &str,
        region_id: &str,
        result_digest: &str,
        result_bytes: &[u8],
    ) -> InteractionOutcome;

    fn report_interaction_outcome(&mut self, report: InteractionResultReport)
        -> InteractionOutcome;

    fn grant_fresh_execution_lease(
        &mut self,
        lease_epoch: u64,
        entry_id: u64,
    ) -> InteractionOutcome;

    fn consume_interaction(&mut self, interaction_id: &str, lease_epoch: u64)
        -> InteractionOutcome;

    fn close_or_fence_turn(&mut self, entry_id: u64) -> InteractionOutcome;

    fn active_lease_epoch(&self) -> Option<u64>;

    fn is_awaiting_interaction(&self) -> bool;

    fn journal_entries(&self) -> usize;

    fn bound_region(&self, interaction_id: &str) -> Option<String>;
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum InteractionStatus {
    Requested,
    RegionPersisted,
    Bound,
}

#[derive(Clone, Debug)]
struct InteractionRecord {
    identity: InteractionIdentity,
    status: InteractionStatus,
    persisted_entry_id: u64,
    result_region_id: Option<String>,
    result_digest: Option<String>,
    bound_entry_id: Option<u64>,
}

/// Bounded D1 Core projection. The constructor creates a private C-01 store
/// because this test-facing API deliberately freezes no storage path or wire
/// ABI; all semantic transitions still go through C-01.
pub struct D1InteractionAuthority {
    storage: D1DurableStorage,
    agent_id: String,
    turn_id: u64,
    last_granted_lease_epoch: u64,
    active_lease_epoch: Option<u64>,
    base_projection_digest: String,
    turn_open: bool,
    awaiting_interaction: bool,
    interactions: HashMap<String, InteractionRecord>,
}

impl D1InteractionAuthority {
    pub fn for_ready_turn(
        agent_id: &str,
        turn_id: u64,
        lease_epoch: u64,
        base_projection_digest: &str,
    ) -> Result<Self, InteractionOutcome> {
        let sequence = INTERACTION_STORAGE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("castor-c03-{}-{sequence}", std::process::id()));
        let storage = D1DurableStorage::open(root)
            .map_err(|_| InteractionOutcome::IntegrityOrProtocolFault)?;
        Ok(Self {
            storage,
            agent_id: agent_id.to_string(),
            turn_id,
            last_granted_lease_epoch: lease_epoch,
            active_lease_epoch: Some(lease_epoch),
            base_projection_digest: base_projection_digest.to_string(),
            turn_open: true,
            awaiting_interaction: false,
            interactions: HashMap::new(),
        })
    }

    fn append(
        &mut self,
        entry_id: u64,
        expected_turn_id: Option<u64>,
        expected_lease_epoch: Option<u64>,
        entry: CoreEntry,
        region_refs: Vec<String>,
    ) -> Result<u64, InteractionOutcome> {
        let request = AppendConditionalRequest {
            agent_id: self.agent_id.clone(),
            entry_id,
            expected_core_epoch: 1,
            expected_agent_generation: None,
            expected_turn_id,
            expected_lease_epoch,
            expected_base_projection_digest: Some(self.base_projection_digest.clone()),
            entry,
            region_refs,
        };
        match self.storage.append_conditional(request) {
            AppendConditionalOutcome::EntryPersisted(proof)
            | AppendConditionalOutcome::AlreadyPersistedSameEntry(proof) => Ok(proof.entry_id),
            AppendConditionalOutcome::RejectedPrecondition { .. } => {
                Err(InteractionOutcome::RejectedPrecondition)
            }
            AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion
            | AppendConditionalOutcome::IntegrityFault => {
                Err(InteractionOutcome::IntegrityOrProtocolFault)
            }
            AppendConditionalOutcome::UnavailableBeforeAck => {
                Err(InteractionOutcome::UnavailableBeforeAck)
            }
        }
    }

    fn identity_matches(left: &InteractionIdentity, right: &InteractionIdentity) -> bool {
        left == right
    }
}

impl InteractionContinuation for D1InteractionAuthority {
    fn request_interaction(&mut self, request: InteractionRequest) -> InteractionOutcome {
        if let Some(existing) = self.interactions.get(&request.identity.interaction_id) {
            return if Self::identity_matches(&existing.identity, &request.identity) {
                InteractionOutcome::InteractionRequestedAck {
                    interaction_id: existing.identity.interaction_id.clone(),
                    persisted_entry_id: existing.persisted_entry_id,
                }
            } else {
                InteractionOutcome::RejectedPrecondition
            };
        }
        if request.identity.agent_id != self.agent_id
            || request.identity.turn_id != self.turn_id
            || !self.turn_open
            || self.awaiting_interaction
            || self.active_lease_epoch != Some(request.lease_epoch)
        {
            return InteractionOutcome::RejectedStaleAuthority;
        }

        let entry = CoreEntry::InteractionRequested {
            turn_id: request.identity.turn_id,
            interaction_id: request.identity.interaction_id.clone(),
            request_digest: request.identity.request_digest.clone(),
            service_id: request.identity.service_id.clone(),
        };
        let persisted_entry_id = match self.append(
            request.entry_id,
            Some(self.turn_id),
            Some(request.lease_epoch),
            entry,
            vec![],
        ) {
            Ok(entry_id) => entry_id,
            Err(outcome) => return outcome,
        };
        self.active_lease_epoch = None;
        self.awaiting_interaction = true;
        self.interactions.insert(
            request.identity.interaction_id.clone(),
            InteractionRecord {
                identity: request.identity.clone(),
                status: InteractionStatus::Requested,
                persisted_entry_id,
                result_region_id: None,
                result_digest: None,
                bound_entry_id: None,
            },
        );
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: request.identity.interaction_id,
            persisted_entry_id,
        }
    }

    fn persist_interaction_result_region(
        &mut self,
        agent_id: &str,
        interaction_id: &str,
        region_id: &str,
        result_digest: &str,
        result_bytes: &[u8],
    ) -> InteractionOutcome {
        let storage_digest = format!("sha256:{:x}", Sha256::digest(result_bytes));
        if result_digest != storage_digest {
            return InteractionOutcome::IntegrityOrProtocolFault;
        }
        let Some(record) = self.interactions.get_mut(interaction_id) else {
            return InteractionOutcome::RejectedPrecondition;
        };
        if agent_id != self.agent_id || record.status != InteractionStatus::Requested {
            return InteractionOutcome::RejectedPrecondition;
        }
        match self.storage.ensure_region(
            region_id,
            &storage_digest,
            result_bytes,
            crate::c01_storage::DurabilityProfile::D1,
        ) {
            EnsureRegionOutcome::Success(_)
            | EnsureRegionOutcome::AlreadyPersistedSameContent(_) => {
                record.status = InteractionStatus::RegionPersisted;
                record.result_region_id = Some(region_id.to_string());
                record.result_digest = Some(storage_digest.clone());
                InteractionOutcome::ResultRegionStored {
                    region_id: region_id.to_string(),
                    result_digest: storage_digest,
                }
            }
            EnsureRegionOutcome::RejectedIdentityConflict | EnsureRegionOutcome::IntegrityFault => {
                InteractionOutcome::IntegrityOrProtocolFault
            }
            EnsureRegionOutcome::UnavailableBeforeAck => InteractionOutcome::UnavailableBeforeAck,
        }
    }

    fn report_interaction_outcome(
        &mut self,
        report: InteractionResultReport,
    ) -> InteractionOutcome {
        let Some(record) = self.interactions.get(&report.identity.interaction_id) else {
            return InteractionOutcome::RejectedPrecondition;
        };
        if !Self::identity_matches(&record.identity, &report.identity) {
            return InteractionOutcome::IntegrityOrProtocolFault;
        }
        if record.status == InteractionStatus::Bound {
            if record.result_region_id.as_deref() == Some(report.region_id.as_str())
                && record.result_digest.as_deref() == Some(report.result_digest.as_str())
            {
                return match record.bound_entry_id {
                    Some(persisted_entry_id) => {
                        InteractionOutcome::AlreadyBoundSameOutcome { persisted_entry_id }
                    }
                    None => InteractionOutcome::IntegrityOrProtocolFault,
                };
            }
            let entry = CoreEntry::ConflictingInteractionOutcomeAppended {
                interaction_id: report.identity.interaction_id,
                conflicting_region_id: report.region_id,
                conflicting_digest: report.result_digest,
            };
            return match self.append(
                report.entry_id,
                self.turn_open.then_some(self.turn_id),
                self.active_lease_epoch,
                entry,
                vec![],
            ) {
                Ok(persisted_entry_id) => {
                    InteractionOutcome::RejectedConflictingOutcome { persisted_entry_id }
                }
                Err(outcome) => outcome,
            };
        }
        if !self.turn_open || !self.awaiting_interaction {
            return InteractionOutcome::RejectedLateOrClosedTurn;
        }
        if record.status != InteractionStatus::RegionPersisted
            || record.result_region_id.as_deref() != Some(report.region_id.as_str())
            || record.result_digest.as_deref() != Some(report.result_digest.as_str())
        {
            return InteractionOutcome::IntegrityOrProtocolFault;
        }
        let Some(region_bytes) = self.storage.read_region(&report.region_id) else {
            return InteractionOutcome::IntegrityOrProtocolFault;
        };
        if format!("sha256:{:x}", Sha256::digest(region_bytes)) != report.result_digest {
            return InteractionOutcome::IntegrityOrProtocolFault;
        }
        let entry = CoreEntry::InteractionBound {
            turn_id: report.identity.turn_id,
            interaction_id: report.identity.interaction_id.clone(),
            region_id: report.region_id.clone(),
            result_digest: report.result_digest,
            disposition: report.disposition,
        };
        let persisted_entry_id = match self.append(
            report.entry_id,
            Some(self.turn_id),
            None,
            entry,
            vec![report.region_id.clone()],
        ) {
            Ok(entry_id) => entry_id,
            Err(outcome) => return outcome,
        };
        let Some(record) = self.interactions.get_mut(&report.identity.interaction_id) else {
            return InteractionOutcome::IntegrityOrProtocolFault;
        };
        record.status = InteractionStatus::Bound;
        record.bound_entry_id = Some(persisted_entry_id);
        self.awaiting_interaction = false;
        InteractionOutcome::Bound {
            persisted_entry_id,
            region_id: report.region_id,
        }
    }

    fn grant_fresh_execution_lease(
        &mut self,
        lease_epoch: u64,
        entry_id: u64,
    ) -> InteractionOutcome {
        if !self.turn_open
            || self.awaiting_interaction
            || !self
                .interactions
                .values()
                .any(|record| record.status == InteractionStatus::Bound)
            || lease_epoch <= self.last_granted_lease_epoch
            || self.active_lease_epoch.is_some()
        {
            return InteractionOutcome::RejectedStaleAuthority;
        }
        let entry = CoreEntry::LeaseGranted {
            turn_id: self.turn_id,
            lease_epoch,
        };
        let persisted_entry_id =
            match self.append(entry_id, Some(self.turn_id), None, entry, vec![]) {
                Ok(entry_id) => entry_id,
                Err(outcome) => return outcome,
            };
        self.active_lease_epoch = Some(lease_epoch);
        self.last_granted_lease_epoch = lease_epoch;
        InteractionOutcome::FreshLeaseGranted {
            persisted_entry_id,
            lease_epoch,
        }
    }

    fn consume_interaction(
        &mut self,
        interaction_id: &str,
        lease_epoch: u64,
    ) -> InteractionOutcome {
        let Some(record) = self.interactions.get(interaction_id) else {
            return InteractionOutcome::RejectedPrecondition;
        };
        if record.status != InteractionStatus::Bound || self.active_lease_epoch != Some(lease_epoch)
        {
            return InteractionOutcome::RejectedStaleAuthority;
        }
        match record.result_region_id.clone() {
            Some(region_id) => InteractionOutcome::InteractionConsumed {
                interaction_id: interaction_id.to_string(),
                region_id,
            },
            None => InteractionOutcome::IntegrityOrProtocolFault,
        }
    }

    fn close_or_fence_turn(&mut self, entry_id: u64) -> InteractionOutcome {
        if !self.turn_open {
            return InteractionOutcome::RejectedPrecondition;
        }
        let persisted_entry_id = match self.append(
            entry_id,
            Some(self.turn_id),
            self.active_lease_epoch,
            CoreEntry::InteractionTurnClosed {
                turn_id: self.turn_id,
            },
            vec![],
        ) {
            Ok(entry_id) => entry_id,
            Err(outcome) => return outcome,
        };
        self.turn_open = false;
        self.awaiting_interaction = false;
        self.active_lease_epoch = None;
        InteractionOutcome::TurnClosedOrFenced { persisted_entry_id }
    }

    fn active_lease_epoch(&self) -> Option<u64> {
        self.active_lease_epoch
    }

    fn is_awaiting_interaction(&self) -> bool {
        self.awaiting_interaction
    }

    fn journal_entries(&self) -> usize {
        self.storage.journal_entries()
    }

    fn bound_region(&self, interaction_id: &str) -> Option<String> {
        self.interactions.get(interaction_id).and_then(|record| {
            (record.status == InteractionStatus::Bound)
                .then(|| record.result_region_id.clone())
                .flatten()
        })
    }
}
