//! Private compilation target for the EPIC-23 C-03 RED contract harness.
//!
//! This module intentionally supplies no interaction-continuation behavior.
//! Its fail-closed outcomes make the Phase 2 charter compile while keeping all
//! behavioral assertions RED until T-294-C refines C-03 onto C-01 and C-02.
//! These names are test-facing only: they freeze neither an SDK nor a wire API.

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

/// Explicitly unavailable pre-implementation target for T-294-B.
pub struct D1InteractionAuthority;

impl D1InteractionAuthority {
    pub fn for_ready_turn(
        _agent_id: &str,
        _turn_id: u64,
        _lease_epoch: u64,
        _base_projection_digest: &str,
    ) -> Self {
        Self
    }
}

impl InteractionContinuation for D1InteractionAuthority {
    fn request_interaction(&mut self, _request: InteractionRequest) -> InteractionOutcome {
        InteractionOutcome::UnavailableBeforeAck
    }

    fn persist_interaction_result_region(
        &mut self,
        _agent_id: &str,
        _interaction_id: &str,
        _region_id: &str,
        _result_digest: &str,
        _result_bytes: &[u8],
    ) -> InteractionOutcome {
        InteractionOutcome::UnavailableBeforeAck
    }

    fn report_interaction_outcome(
        &mut self,
        _report: InteractionResultReport,
    ) -> InteractionOutcome {
        InteractionOutcome::UnavailableBeforeAck
    }

    fn grant_fresh_execution_lease(
        &mut self,
        _lease_epoch: u64,
        _entry_id: u64,
    ) -> InteractionOutcome {
        InteractionOutcome::UnavailableBeforeAck
    }

    fn consume_interaction(
        &mut self,
        _interaction_id: &str,
        _lease_epoch: u64,
    ) -> InteractionOutcome {
        InteractionOutcome::UnavailableBeforeAck
    }

    fn close_or_fence_turn(&mut self, _entry_id: u64) -> InteractionOutcome {
        InteractionOutcome::UnavailableBeforeAck
    }

    fn active_lease_epoch(&self) -> Option<u64> {
        None
    }

    fn is_awaiting_interaction(&self) -> bool {
        false
    }

    fn journal_entries(&self) -> usize {
        0
    }

    fn bound_region(&self, _interaction_id: &str) -> Option<String> {
        None
    }
}
