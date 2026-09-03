//! Governed-turn composition contract vocabulary.
//!
//! This is a test-facing Phase-2 boundary. The Profile-D1 placeholder keeps
//! every state-changing operation fail-closed until a later phase composes the
//! C-01 through C-05 authorities into a durable governed turn.

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TurnAuthority {
    pub agent_id: String,
    pub core_epoch: u64,
    pub agent_generation: u64,
    pub incarnation_id: String,
    pub turn_id: u64,
    pub lease_epoch: u64,
    pub base_projection_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StartTurnRequest {
    pub authority: TurnAuthority,
    pub input_region_id: String,
    pub input_digest: String,
    pub policy_digest: String,
    pub entry_id: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TurnDisposition {
    Completed,
    Aborted,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompletionReport {
    pub authority: TurnAuthority,
    pub output_region_id: String,
    pub output_digest: String,
    pub interaction_digest: String,
    pub settlement_digest: String,
    pub successor_projection_digest: String,
    pub disposition: TurnDisposition,
    pub entry_id: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GovernedTurnOutcome {
    TurnStarted {
        entry_id: u64,
    },
    TurnCommitted {
        entry_id: u64,
        successor_projection_digest: String,
    },
    AlreadyCommittedSameCompletion {
        entry_id: u64,
    },
    ConflictingCompletionQuarantined {
        entry_id: u64,
    },
    TurnAborted {
        entry_id: u64,
    },
    RejectedStaleAuthority {
        current_generation: u64,
        current_lease_epoch: Option<u64>,
    },
    RejectedCurrentState,
    RejectedPrecondition,
    IntegrityOrProtocolFault,
    RejectedPreimplementation,
}

/// Test-facing semantic boundary. It freezes neither a wire API nor the
/// internal representation of the composed C-01 through C-05 projections.
pub trait GovernedTurnComposition {
    fn start_turn(&mut self, request: StartTurnRequest) -> GovernedTurnOutcome;
    fn complete_turn(&mut self, report: CompletionReport) -> GovernedTurnOutcome;
    fn abort_turn(&mut self, authority: TurnAuthority, entry_id: u64) -> GovernedTurnOutcome;
    fn fence_generation(&mut self, new_generation: u64);
    fn reconstruct_after_crash(&mut self);
    fn active_lease_epoch(&self) -> Option<u64>;
    fn turn_is_unresolved(&self, turn_id: u64) -> bool;
}

/// Deliberately unavailable Profile-D1 target for the RED composition charter.
#[derive(Default)]
pub struct D1GovernedTurnComposition;

impl D1GovernedTurnComposition {
    pub fn for_test(_agent_id: &str, _core_epoch: u64, _generation: u64) -> Self {
        Self
    }
}

impl GovernedTurnComposition for D1GovernedTurnComposition {
    fn start_turn(&mut self, _request: StartTurnRequest) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    fn complete_turn(&mut self, _report: CompletionReport) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    fn abort_turn(&mut self, _authority: TurnAuthority, _entry_id: u64) -> GovernedTurnOutcome {
        GovernedTurnOutcome::RejectedPreimplementation
    }

    fn fence_generation(&mut self, _new_generation: u64) {}

    fn reconstruct_after_crash(&mut self) {}

    fn active_lease_epoch(&self) -> Option<u64> {
        None
    }

    fn turn_is_unresolved(&self, _turn_id: u64) -> bool {
        false
    }
}
