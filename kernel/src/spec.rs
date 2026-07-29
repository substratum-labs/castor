//! Pure Verus model of the state in `specs/tla/CastorKernel.tla`.
//!
//! This module deliberately models only values and predicates.  Syscall
//! transitions and contracts for the Rust/Python API belong to later work.

use vstd::prelude::*;

verus! {

/// Effects configured by the current TLA+ model.
pub enum Effect {
    EffectA,
    EffectB,
}

/// Capabilities configured by the current TLA+ model.
pub enum Capability {
    CapA,
    CapB,
}

/// The three entries that can be appended to the journal.
pub enum JournalRecordType {
    Proposed,
    Committed,
    Rejected,
}

/// A typed entry in the append-only journal.
pub struct JournalEntry {
    pub record_type: JournalRecordType,
    pub effect: Effect,
}

/// The agent execution modes in the TLA+ state space.
pub enum AgentState {
    Running,
    PendingHitl,
    Suspended,
}

/// The complete pure kernel state modeled by T-275.
pub struct KernelState {
    pub journal: Seq<JournalEntry>,
    pub capabilities: Set<Capability>,
    pub agent_state: AgentState,
    pub cursor: nat,
}

/// TLA+'s `RequireCap` relation for the configured effects.
pub open spec fn required_capabilities(effect: Effect) -> Set<Capability> {
    match effect {
        Effect::EffectA => Set::empty().insert(Capability::CapA),
        Effect::EffectB => Set::empty().insert(Capability::CapB),
    }
}

/// An effect is authorized precisely when every required capability is held.
pub open spec fn is_authorized(state: KernelState, effect: Effect) -> bool {
    required_capabilities(effect).subset_of(state.capabilities)
}

/// Proposal is permitted only for a running, authorized agent.
pub open spec fn proposal_is_authorized(state: KernelState, effect: Effect) -> bool {
    state.agent_state == AgentState::Running && is_authorized(state, effect)
}

/// Commit repeats authorization against the current capabilities (TOCTOU defense).
pub open spec fn commit_is_authorized(state: KernelState, effect: Effect) -> bool {
    state.agent_state == AgentState::PendingHitl && is_authorized(state, effect)
}

/// The state invariant beyond the static Verus types: a cursor never exceeds
/// the number of journal records available to replay.
pub open spec fn state_is_valid(state: KernelState) -> bool {
    state.cursor <= state.journal.len()
}

/// TLA+'s initial state, represented as a pure value.
pub open spec fn initial_state() -> KernelState {
    KernelState {
        journal: Seq::empty(),
        capabilities: Set::empty(),
        agent_state: AgentState::Running,
        cursor: 0,
    }
}

proof fn initial_state_is_valid() {
    assert(state_is_valid(initial_state()));
}

proof fn proposal_and_commit_check_the_same_capability_relation(
    state: KernelState,
    effect: Effect,
) {
    assert(proposal_is_authorized(state, effect)
        ==> is_authorized(state, effect));
    assert(commit_is_authorized(state, effect)
        ==> is_authorized(state, effect));
}

} // verus!
