//! C-02 single-node D1 execution authority refined onto C-01.
//!
//! The authority projection is volatile convenience state. C-01 conditional
//! entries are the only semantic linearization points: this module installs a
//! lease, fence, or successor projection only after an `EntryPersisted` proof
//! (or an exact stable-entry recovery) is available.

use crate::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurableStorage, PersistedEntryProof,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuthorityTuple {
    pub agent_id: String,
    pub core_epoch: u64,
    pub agent_generation: u64,
    pub incarnation_id: String,
    pub turn_id: u64,
    pub lease_epoch: u64,
    pub base_projection_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdvanceTurnRequest {
    pub tuple: AuthorityTuple,
    pub entry_id: u64,
    pub successor_projection_digest: String,
    /// Core data only. This C-02 slice never arms or dispatches it.
    pub action_manifest_digest: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ExecutionOutcome {
    IncarnationBound {
        incarnation_id: String,
        agent_generation: u64,
    },
    LeaseGranted {
        persisted_entry_id: u64,
        permits_direct_durable_write: bool,
        permits_direct_effect_dispatch: bool,
    },
    TurnCommitted {
        persisted_entry_id: u64,
        successor_projection_digest: String,
    },
    GenerationFenced {
        persisted_entry_id: u64,
        agent_generation: u64,
    },
    RejectedStaleAuthority {
        current_generation: u64,
        current_lease_epoch: Option<u64>,
    },
    RejectedPrecondition,
    UnavailableBeforeAck,
    IntegrityOrProtocolFault,
}

/// Test-facing semantic boundary; no wire or SDK API is frozen here.
pub trait ExecutionAuthority {
    fn bind_incarnation(
        &mut self,
        incarnation_id: &str,
        expected_generation: u64,
    ) -> ExecutionOutcome;

    fn grant_execution_lease(&mut self, tuple: AuthorityTuple, entry_id: u64) -> ExecutionOutcome;

    fn revoke_or_fence_execution(
        &mut self,
        expected_core_epoch: u64,
        current_generation: u64,
        new_generation: u64,
        entry_id: u64,
    ) -> ExecutionOutcome;

    fn advance_turn(&mut self, request: AdvanceTurnRequest) -> ExecutionOutcome;

    fn current_lease_permissions(&self) -> Option<(bool, bool)>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LeaseState {
    Idle,
    Bound,
    Leased,
    Fenced,
}

/// The bounded Profile-D1 implementation. It is deliberately not wired to an
/// RPC endpoint, Roche, a scheduler, or a C-04 adapter.
pub struct D1ExecutionAuthority {
    storage: D1DurableStorage,
    agent_id: String,
    core_epoch: u64,
    agent_generation: u64,
    bound_incarnation_id: Option<String>,
    ready_turn_id: u64,
    active_turn_id: Option<u64>,
    active_lease_epoch: Option<u64>,
    next_lease_epoch: u64,
    base_projection_digest: String,
    lease_state: LeaseState,
}

impl D1ExecutionAuthority {
    pub fn for_ready_turn(
        storage: D1DurableStorage,
        agent_id: &str,
        core_epoch: u64,
        agent_generation: u64,
        ready_turn_id: u64,
        next_lease_epoch: u64,
        base_projection_digest: &str,
    ) -> Self {
        Self {
            storage,
            agent_id: agent_id.to_string(),
            core_epoch,
            agent_generation,
            bound_incarnation_id: None,
            ready_turn_id,
            active_turn_id: None,
            active_lease_epoch: None,
            next_lease_epoch,
            base_projection_digest: base_projection_digest.to_string(),
            lease_state: LeaseState::Idle,
        }
    }

    /// Read-only inspection for the Rust contract harness; this is not Agent
    /// authority and deliberately returns no mutable storage handle.
    pub fn storage(&self) -> &D1DurableStorage {
        &self.storage
    }

    fn stale(&self) -> ExecutionOutcome {
        ExecutionOutcome::RejectedStaleAuthority {
            current_generation: self.agent_generation,
            current_lease_epoch: self.active_lease_epoch,
        }
    }

    fn tuple_matches_common(&self, tuple: &AuthorityTuple) -> bool {
        tuple.agent_id == self.agent_id
            && tuple.core_epoch == self.core_epoch
            && tuple.agent_generation == self.agent_generation
            && self.bound_incarnation_id.as_deref() == Some(tuple.incarnation_id.as_str())
            && tuple.base_projection_digest == self.base_projection_digest
    }

    fn matching_persisted_entry(
        &self,
        request: &AppendConditionalRequest,
    ) -> Option<PersistedEntryProof> {
        let proof = self
            .storage
            .read_entry(&request.agent_id, request.entry_id)?;
        (self.storage.resolve_entry(&proof).as_ref() == Some(request)).then_some(proof)
    }

    fn append_or_recover(
        &mut self,
        request: AppendConditionalRequest,
    ) -> Result<PersistedEntryProof, ExecutionOutcome> {
        match self.storage.append_conditional(request.clone()) {
            AppendConditionalOutcome::EntryPersisted(proof)
            | AppendConditionalOutcome::AlreadyPersistedSameEntry(proof) => Ok(proof),
            AppendConditionalOutcome::RejectedPrecondition { .. } => self
                .matching_persisted_entry(&request)
                .ok_or(ExecutionOutcome::RejectedPrecondition),
            AppendConditionalOutcome::UnavailableBeforeAck => {
                Err(ExecutionOutcome::UnavailableBeforeAck)
            }
            AppendConditionalOutcome::IntegrityFault
            | AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion => {
                Err(ExecutionOutcome::IntegrityOrProtocolFault)
            }
        }
    }

    fn lease_request(&self, tuple: &AuthorityTuple, entry_id: u64) -> AppendConditionalRequest {
        AppendConditionalRequest {
            agent_id: tuple.agent_id.clone(),
            entry_id,
            expected_core_epoch: tuple.core_epoch,
            expected_agent_generation: Some(tuple.agent_generation),
            expected_turn_id: Some(tuple.turn_id),
            expected_lease_epoch: Some(tuple.lease_epoch),
            expected_base_projection_digest: Some(tuple.base_projection_digest.clone()),
            entry: CoreEntry::LeaseGranted {
                turn_id: tuple.turn_id,
                lease_epoch: tuple.lease_epoch,
            },
            region_refs: vec![],
        }
    }
}

impl ExecutionAuthority for D1ExecutionAuthority {
    fn bind_incarnation(
        &mut self,
        incarnation_id: &str,
        expected_generation: u64,
    ) -> ExecutionOutcome {
        if expected_generation != self.agent_generation {
            return self.stale();
        }
        if matches!(self.lease_state, LeaseState::Bound | LeaseState::Leased)
            && self.bound_incarnation_id.as_deref() != Some(incarnation_id)
        {
            return ExecutionOutcome::RejectedPrecondition;
        }
        if self.lease_state == LeaseState::Leased {
            return ExecutionOutcome::RejectedPrecondition;
        }

        self.bound_incarnation_id = Some(incarnation_id.to_string());
        self.lease_state = LeaseState::Bound;
        ExecutionOutcome::IncarnationBound {
            incarnation_id: incarnation_id.to_string(),
            agent_generation: self.agent_generation,
        }
    }

    fn grant_execution_lease(&mut self, tuple: AuthorityTuple, entry_id: u64) -> ExecutionOutcome {
        let request = self.lease_request(&tuple, entry_id);
        if !self.tuple_matches_common(&tuple) {
            return self.stale();
        }
        if let Some(proof) = self.matching_persisted_entry(&request) {
            self.active_turn_id = Some(tuple.turn_id);
            self.active_lease_epoch = Some(tuple.lease_epoch);
            self.lease_state = LeaseState::Leased;
            return ExecutionOutcome::LeaseGranted {
                persisted_entry_id: proof.entry_id,
                permits_direct_durable_write: false,
                permits_direct_effect_dispatch: false,
            };
        }
        if self.lease_state != LeaseState::Bound
            || tuple.turn_id != self.ready_turn_id
            || tuple.lease_epoch != self.next_lease_epoch
        {
            return ExecutionOutcome::RejectedPrecondition;
        }

        match self.append_or_recover(request) {
            Ok(proof) => {
                self.active_turn_id = Some(tuple.turn_id);
                self.active_lease_epoch = Some(tuple.lease_epoch);
                self.lease_state = LeaseState::Leased;
                ExecutionOutcome::LeaseGranted {
                    persisted_entry_id: proof.entry_id,
                    permits_direct_durable_write: false,
                    permits_direct_effect_dispatch: false,
                }
            }
            Err(ExecutionOutcome::RejectedPrecondition) => ExecutionOutcome::RejectedPrecondition,
            Err(other) => other,
        }
    }

    fn revoke_or_fence_execution(
        &mut self,
        expected_core_epoch: u64,
        current_generation: u64,
        new_generation: u64,
        entry_id: u64,
    ) -> ExecutionOutcome {
        if expected_core_epoch != self.core_epoch || current_generation != self.agent_generation {
            return self.stale();
        }
        if new_generation <= current_generation {
            return ExecutionOutcome::RejectedPrecondition;
        }

        let request = AppendConditionalRequest {
            agent_id: self.agent_id.clone(),
            entry_id,
            expected_core_epoch,
            expected_agent_generation: Some(current_generation),
            expected_turn_id: self.active_turn_id,
            expected_lease_epoch: self.active_lease_epoch,
            expected_base_projection_digest: Some(self.base_projection_digest.clone()),
            entry: CoreEntry::FenceRevoked {
                generation: new_generation,
            },
            region_refs: vec![],
        };
        match self.append_or_recover(request) {
            Ok(proof) => {
                self.agent_generation = new_generation;
                self.bound_incarnation_id = None;
                self.active_turn_id = None;
                self.active_lease_epoch = None;
                self.lease_state = LeaseState::Fenced;
                ExecutionOutcome::GenerationFenced {
                    persisted_entry_id: proof.entry_id,
                    agent_generation: new_generation,
                }
            }
            Err(ExecutionOutcome::RejectedPrecondition) => self.stale(),
            Err(other) => other,
        }
    }

    fn advance_turn(&mut self, request: AdvanceTurnRequest) -> ExecutionOutcome {
        let entry = AppendConditionalRequest {
            agent_id: request.tuple.agent_id.clone(),
            entry_id: request.entry_id,
            expected_core_epoch: request.tuple.core_epoch,
            expected_agent_generation: Some(request.tuple.agent_generation),
            expected_turn_id: Some(request.tuple.turn_id),
            expected_lease_epoch: Some(request.tuple.lease_epoch),
            expected_base_projection_digest: Some(request.tuple.base_projection_digest.clone()),
            entry: CoreEntry::TurnCommitted {
                turn_id: request.tuple.turn_id,
                successor_projection_digest: Some(request.successor_projection_digest.clone()),
                action_manifest_digest: request.action_manifest_digest.clone(),
                action_manifest: vec![],
                cap_id: None,
            },
            region_refs: vec![],
        };
        if !self.tuple_matches_common(&request.tuple)
            || self.lease_state != LeaseState::Leased
            || self.active_turn_id != Some(request.tuple.turn_id)
            || self.active_lease_epoch != Some(request.tuple.lease_epoch)
        {
            return self.stale();
        }
        if let Some(proof) = self.matching_persisted_entry(&entry) {
            self.base_projection_digest = request.successor_projection_digest.clone();
            self.active_turn_id = None;
            self.active_lease_epoch = None;
            self.lease_state = LeaseState::Idle;
            return ExecutionOutcome::TurnCommitted {
                persisted_entry_id: proof.entry_id,
                successor_projection_digest: request.successor_projection_digest,
            };
        }
        match self.append_or_recover(entry) {
            Ok(proof) => {
                self.base_projection_digest = request.successor_projection_digest.clone();
                self.active_turn_id = None;
                self.active_lease_epoch = None;
                self.lease_state = LeaseState::Idle;
                ExecutionOutcome::TurnCommitted {
                    persisted_entry_id: proof.entry_id,
                    successor_projection_digest: request.successor_projection_digest,
                }
            }
            Err(ExecutionOutcome::RejectedPrecondition) => self.stale(),
            Err(other) => other,
        }
    }

    fn current_lease_permissions(&self) -> Option<(bool, bool)> {
        (self.lease_state == LeaseState::Leased).then_some((false, false))
    }
}
