//! These functions are pure model transitions.  The accompanying proof
//! functions are their contracts: each preserves the state invariant and
//! specifies the fields that the corresponding TLA+ transition changes.

use vstd::prelude::*;

use crate::spec::{
    commit_is_authorized, proposal_is_authorized, state_is_valid, AgentState,
    Capability, Effect, JournalEntry, JournalRecordType, KernelState,
};

verus! {

/// TLA+'s `GrantCapability(c)` transition.
pub open spec fn grant_capability(state: KernelState, capability: Capability) -> KernelState {
    KernelState {
        journal: state.journal,
        capabilities: state.capabilities.insert(capability),
        agent_state: state.agent_state,
        cursor: state.cursor,
    }
}

/// TLA+'s `RevokeCapability(c)` transition.
pub open spec fn revoke_capability(state: KernelState, capability: Capability) -> KernelState {
    KernelState {
        journal: state.journal,
        capabilities: state.capabilities.remove(capability),
        agent_state: state.agent_state,
        cursor: state.cursor,
    }
}

/// TLA+'s `Syscall_Propose(e)` transition.
pub open spec fn syscall_propose(state: KernelState, effect: Effect) -> KernelState {
    KernelState {
        journal: state.journal.push(JournalEntry {
            record_type: JournalRecordType::Proposed,
            effect,
        }),
        capabilities: state.capabilities,
        agent_state: AgentState::PendingHitl,
        cursor: state.cursor,
    }
}

/// TLA+'s `Syscall_Commit(e)` transition, including the TOCTOU recheck.
pub open spec fn syscall_commit(state: KernelState, effect: Effect) -> KernelState {
    KernelState {
        journal: state.journal.push(JournalEntry {
            record_type: JournalRecordType::Committed,
            effect,
        }),
        capabilities: state.capabilities,
        agent_state: AgentState::Running,
        cursor: state.journal.len() + 1,
    }
}

/// TLA+'s `Syscall_Reject(e)` transition.
pub open spec fn syscall_reject(state: KernelState, effect: Effect) -> KernelState {
    KernelState {
        journal: state.journal.push(JournalEntry {
            record_type: JournalRecordType::Rejected,
            effect,
        }),
        capabilities: state.capabilities,
        agent_state: AgentState::Running,
        cursor: state.journal.len() + 1,
    }
}

/// TLA+'s `Fault_Preempt` transition.
pub open spec fn fault_preempt(state: KernelState) -> KernelState {
    KernelState {
        journal: state.journal,
        capabilities: state.capabilities,
        agent_state: AgentState::Suspended,
        cursor: state.cursor,
    }
}

/// TLA+'s `Resume_Execution` transition.
pub open spec fn resume_execution(state: KernelState) -> KernelState {
    KernelState {
        journal: state.journal,
        capabilities: state.capabilities,
        agent_state: AgentState::Running,
        cursor: state.journal.len(),
    }
}

pub proof fn grant_capability_contract(state: KernelState, capability: Capability)
    requires state_is_valid(state),
    ensures
        state_is_valid(grant_capability(state, capability)),
        grant_capability(state, capability).journal == state.journal,
        grant_capability(state, capability).agent_state == state.agent_state,
        grant_capability(state, capability).cursor == state.cursor,
        grant_capability(state, capability).capabilities
            == state.capabilities.insert(capability),
{}

pub proof fn revoke_capability_contract(state: KernelState, capability: Capability)
    requires state_is_valid(state),
    ensures
        state_is_valid(revoke_capability(state, capability)),
        revoke_capability(state, capability).journal == state.journal,
        revoke_capability(state, capability).agent_state == state.agent_state,
        revoke_capability(state, capability).cursor == state.cursor,
        revoke_capability(state, capability).capabilities
            == state.capabilities.remove(capability),
{}

pub proof fn syscall_propose_contract(state: KernelState, effect: Effect)
    requires
        state_is_valid(state),
        proposal_is_authorized(state, effect),
    ensures
        state_is_valid(syscall_propose(state, effect)),
        syscall_propose(state, effect).journal == state.journal.push(JournalEntry {
            record_type: JournalRecordType::Proposed,
            effect,
        }),
        syscall_propose(state, effect).capabilities == state.capabilities,
        syscall_propose(state, effect).agent_state == AgentState::PendingHitl,
        syscall_propose(state, effect).cursor == state.cursor,
{}

pub proof fn syscall_commit_contract(state: KernelState, effect: Effect)
    requires
        state_is_valid(state),
        commit_is_authorized(state, effect),
    ensures
        state_is_valid(syscall_commit(state, effect)),
        syscall_commit(state, effect).journal == state.journal.push(JournalEntry {
            record_type: JournalRecordType::Committed,
            effect,
        }),
        syscall_commit(state, effect).capabilities == state.capabilities,
        syscall_commit(state, effect).agent_state == AgentState::Running,
        syscall_commit(state, effect).cursor == syscall_commit(state, effect).journal.len(),
{}

pub proof fn syscall_reject_contract(state: KernelState, effect: Effect)
    requires
        state_is_valid(state),
        state.agent_state == AgentState::PendingHitl,
    ensures
        state_is_valid(syscall_reject(state, effect)),
        syscall_reject(state, effect).journal == state.journal.push(JournalEntry {
            record_type: JournalRecordType::Rejected,
            effect,
        }),
        syscall_reject(state, effect).capabilities == state.capabilities,
        syscall_reject(state, effect).agent_state == AgentState::Running,
        syscall_reject(state, effect).cursor == syscall_reject(state, effect).journal.len(),
{}

pub proof fn fault_preempt_contract(state: KernelState)
    requires
        state_is_valid(state),
        state.agent_state == AgentState::Running || state.agent_state == AgentState::PendingHitl,
    ensures
        state_is_valid(fault_preempt(state)),
        fault_preempt(state).journal == state.journal,
        fault_preempt(state).capabilities == state.capabilities,
        fault_preempt(state).agent_state == AgentState::Suspended,
        fault_preempt(state).cursor == state.cursor,
{}

pub proof fn resume_execution_contract(state: KernelState)
    requires
        state_is_valid(state),
        state.agent_state == AgentState::Suspended,
    ensures
        state_is_valid(resume_execution(state)),
        resume_execution(state).journal == state.journal,
        resume_execution(state).capabilities == state.capabilities,
        resume_execution(state).agent_state == AgentState::Running,
        resume_execution(state).cursor == resume_execution(state).journal.len(),
{}

} // verus!
