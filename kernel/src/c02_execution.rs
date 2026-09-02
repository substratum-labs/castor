//! Private compilation target for the EPIC-22 C-02 RED contract harness.
//!
//! This module deliberately contains no execution-authority implementation:
//! it neither owns C-01 storage nor records a projection.  Its purpose is to
//! make the Phase-B behavioral tests compile and fail against an explicit
//! unavailable boundary.  T-291-C must replace this target with the real
//! C-02-to-C-01 refinement; these Rust names are not an SDK, wire API, or
//! frozen L4 representation.

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
    /// Core data only.  The C-02 boundary never arms or dispatches it.
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

/// Test-facing semantic boundary, intentionally not a stable public API.
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

/// A fail-closed Phase-B stub.  It is not connected to `Castord`.
#[derive(Default)]
pub struct PreImplementationExecutionAuthority;

impl PreImplementationExecutionAuthority {
    pub fn for_ready_turn(
        _agent_id: &str,
        _core_epoch: u64,
        _agent_generation: u64,
        _turn_id: u64,
        _next_lease_epoch: u64,
        _base_projection_digest: &str,
    ) -> Self {
        Self
    }
}

impl ExecutionAuthority for PreImplementationExecutionAuthority {
    fn bind_incarnation(
        &mut self,
        _incarnation_id: &str,
        _expected_generation: u64,
    ) -> ExecutionOutcome {
        ExecutionOutcome::UnavailableBeforeAck
    }

    fn grant_execution_lease(
        &mut self,
        _tuple: AuthorityTuple,
        _entry_id: u64,
    ) -> ExecutionOutcome {
        ExecutionOutcome::UnavailableBeforeAck
    }

    fn revoke_or_fence_execution(
        &mut self,
        _expected_core_epoch: u64,
        _current_generation: u64,
        _new_generation: u64,
        _entry_id: u64,
    ) -> ExecutionOutcome {
        ExecutionOutcome::UnavailableBeforeAck
    }

    fn advance_turn(&mut self, _request: AdvanceTurnRequest) -> ExecutionOutcome {
        ExecutionOutcome::UnavailableBeforeAck
    }

    fn current_lease_permissions(&self) -> Option<(bool, bool)> {
        None
    }
}
