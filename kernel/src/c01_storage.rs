//! C-01 Durable Storage Contract Types and Interfaces (L5 Target)

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DurabilityProfile {
    D1, // Single-node local disk
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RegionPersisted {
    pub region_ref: String,
    pub content_digest: String,
    pub profile: DurabilityProfile,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EnsureRegionOutcome {
    Success(RegionPersisted),
    AlreadyPersistedSameContent(RegionPersisted),
    RejectedIdentityConflict,
    UnavailableBeforeAck,
    IntegrityFault,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PersistedEntryProof {
    pub agent_id: String,
    pub entry_id: u64,
    pub entry_digest: String,
    pub entry_kind: String,
    pub durability_profile: DurabilityProfile,
    pub expected_projection_digest: String,
    pub referenced_region_digests: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CoreEntry {
    TurnCommitted {
        turn_id: u64,
    },
    AttemptArmed {
        action_id: String,
        attempt_id: u64,
        request_digest: String,
    },
    DispatchAttempt {
        action_id: String,
        attempt_id: u64,
        adapter_id: String,
    },
    FenceRevoked {
        generation: u64,
    },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AppendConditionalOutcome {
    EntryPersisted(PersistedEntryProof),
    AlreadyPersistedSameEntry(PersistedEntryProof),
    RejectedPrecondition {
        current_projection_hint: Option<String>,
    },
    RejectedMissingOrUnpersistedRegion,
    UnavailableBeforeAck,
    IntegrityFault,
}

pub trait DurableStorage {
    fn ensure_region(
        &mut self,
        region_ref: &str,
        content_digest: &str,
        profile: DurabilityProfile,
    ) -> EnsureRegionOutcome;

    fn append_conditional(
        &mut self,
        agent_id: &str,
        expected_epoch: u64,
        entry: CoreEntry,
        region_refs: &[String],
    ) -> AppendConditionalOutcome;

    fn read_entry(&self, agent_id: &str, entry_id: u64) -> Option<PersistedEntryProof>;
}

/// Pre-implementation stub for DurableStorage (fails until Phase 3 implementation)
#[derive(Default)]
pub struct PreImplementationDurableStorage;

impl PreImplementationDurableStorage {
    pub fn new() -> Self {
        Self
    }
}

impl DurableStorage for PreImplementationDurableStorage {
    fn ensure_region(
        &mut self,
        _region_ref: &str,
        _content_digest: &str,
        _profile: DurabilityProfile,
    ) -> EnsureRegionOutcome {
        EnsureRegionOutcome::UnavailableBeforeAck
    }

    fn append_conditional(
        &mut self,
        _agent_id: &str,
        _expected_epoch: u64,
        _entry: CoreEntry,
        _region_refs: &[String],
    ) -> AppendConditionalOutcome {
        AppendConditionalOutcome::UnavailableBeforeAck
    }

    fn read_entry(&self, _agent_id: &str, _entry_id: u64) -> Option<PersistedEntryProof> {
        None
    }
}
