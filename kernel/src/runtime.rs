//! Executable, verified syscall state transitions for Rust consumers.

use std::collections::BTreeSet;

use vstd::prelude::*;

use crate::spec;

verus! {

broadcast use {
    vstd::std_specs::btree::group_btree_axioms,
    vstd::std_specs::vec::group_vec_axioms,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Effect {
    EffectA,
    EffectB,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Capability {
    CapA,
    CapB,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JournalRecordType {
    Proposed,
    Committed,
    Rejected,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct JournalEntry {
    pub record_type: JournalRecordType,
    pub effect: Effect,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AgentState {
    Running,
    PendingHitl,
    Suspended,
}

#[derive(Clone, Debug, PartialEq, Eq)]
#[verifier::allow(autoderive_clone_without_spec)]
pub struct KernelState {
    pub journal: Vec<JournalEntry>,
    pub capabilities: BTreeSet<Capability>,
    pub agent_state: AgentState,
    pub cursor: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SyscallError {
    Unauthorized,
    InvalidState,
}

impl View for Effect {
    type V = spec::Effect;

    open spec fn view(&self) -> spec::Effect {
        match self {
            Effect::EffectA => spec::Effect::EffectA,
            Effect::EffectB => spec::Effect::EffectB,
        }
    }
}

impl View for Capability {
    type V = spec::Capability;

    open spec fn view(&self) -> spec::Capability {
        match self {
            Capability::CapA => spec::Capability::CapA,
            Capability::CapB => spec::Capability::CapB,
        }
    }
}

impl View for JournalRecordType {
    type V = spec::JournalRecordType;

    open spec fn view(&self) -> spec::JournalRecordType {
        match self {
            JournalRecordType::Proposed => spec::JournalRecordType::Proposed,
            JournalRecordType::Committed => spec::JournalRecordType::Committed,
            JournalRecordType::Rejected => spec::JournalRecordType::Rejected,
        }
    }
}

impl View for JournalEntry {
    type V = spec::JournalEntry;

    open spec fn view(&self) -> spec::JournalEntry {
        spec::JournalEntry {
            record_type: self.record_type@,
            effect: self.effect@,
        }
    }
}

impl View for AgentState {
    type V = spec::AgentState;

    open spec fn view(&self) -> spec::AgentState {
        match self {
            AgentState::Running => spec::AgentState::Running,
            AgentState::PendingHitl => spec::AgentState::PendingHitl,
            AgentState::Suspended => spec::AgentState::Suspended,
        }
    }
}

impl View for KernelState {
    type V = spec::KernelState;

    open spec fn view(&self) -> spec::KernelState {
        spec::KernelState {
            journal: self.journal@.map(|_index: int, entry: JournalEntry| entry@),
            capabilities: self.capabilities@.map(|capability: Capability| capability@),
            agent_state: self.agent_state@,
            cursor: self.cursor as nat,
        }
    }
}

pub open spec fn runtime_state_is_valid(state: KernelState) -> bool {
    state.cursor as nat <= state.journal@.len()
}

impl KernelState {
    pub exec fn new() -> (state: KernelState)
        ensures
            runtime_state_is_valid(state),
            state.agent_state == AgentState::Running,
    {
        KernelState {
            journal: Vec::new(),
            capabilities: BTreeSet::new(),
            agent_state: AgentState::Running,
            cursor: 0,
        }
    }
}

pub exec fn grant_capability(state: KernelState, capability: Capability) -> (next: KernelState)
    ensures
        next.journal@ == state.journal@,
        next.agent_state == state.agent_state,
        next.cursor == state.cursor,
{
    let mut next = state;
    next.capabilities.insert(capability);
    next
}

pub exec fn revoke_capability(state: KernelState, capability: Capability) -> (next: KernelState)
    ensures
        next.journal@ == state.journal@,
        next.agent_state == state.agent_state,
        next.cursor == state.cursor,
{
    let mut next = state;
    next.capabilities.remove(&capability);
    next
}

pub exec fn syscall_propose(state: KernelState, effect: Effect) -> (result: Result<KernelState, SyscallError>)
    ensures
        result.is_Ok() ==> {
            &&& runtime_state_is_valid(result.unwrap())
            &&& result.unwrap().agent_state == AgentState::PendingHitl
            &&& result.unwrap().journal@.len() == state.journal@.len() + 1
        },
{
    if state.cursor > state.journal.len() {
        return Err(SyscallError::InvalidState);
    }
    if state.agent_state != AgentState::Running {
        return Err(SyscallError::InvalidState);
    }
    if !state.capabilities.contains(&required_capability(effect)) {
        return Err(SyscallError::Unauthorized);
    }

    let mut next = state;
    next.journal.push(JournalEntry { record_type: JournalRecordType::Proposed, effect });
    next.agent_state = AgentState::PendingHitl;
    Ok(next)
}

/// Commits only after re-checking the capability on the current pending state.
pub exec fn syscall_commit(state: KernelState, effect: Effect) -> (result: Result<KernelState, SyscallError>)
    ensures
        result.is_Ok() ==> {
            &&& runtime_state_is_valid(result.unwrap())
            &&& result.unwrap().agent_state == AgentState::Running
            &&& result.unwrap().cursor == result.unwrap().journal@.len()
        },
{
    if state.cursor > state.journal.len() {
        return Err(SyscallError::InvalidState);
    }
    if state.agent_state != AgentState::PendingHitl {
        return Err(SyscallError::InvalidState);
    }
    if !state.capabilities.contains(&required_capability(effect)) {
        return Err(SyscallError::Unauthorized);
    }

    let mut next = state;
    next.journal.push(JournalEntry { record_type: JournalRecordType::Committed, effect });
    next.agent_state = AgentState::Running;
    next.cursor = next.journal.len();
    Ok(next)
}

pub exec fn syscall_reject(state: KernelState, effect: Effect) -> (result: Result<KernelState, SyscallError>)
    ensures
        result.is_Ok() ==> {
            &&& runtime_state_is_valid(result.unwrap())
            &&& result.unwrap().agent_state == AgentState::Running
            &&& result.unwrap().cursor == result.unwrap().journal@.len()
        },
{
    if state.cursor > state.journal.len() {
        return Err(SyscallError::InvalidState);
    }
    if state.agent_state != AgentState::PendingHitl {
        return Err(SyscallError::InvalidState);
    }

    let mut next = state;
    next.journal.push(JournalEntry { record_type: JournalRecordType::Rejected, effect });
    next.agent_state = AgentState::Running;
    next.cursor = next.journal.len();
    Ok(next)
}

pub exec fn fault_preempt(state: KernelState) -> (next: KernelState)
    ensures
        next.journal@ == state.journal@,
        next.cursor == state.cursor,
        next.agent_state == AgentState::Suspended,
{
    let mut next = state;
    next.agent_state = AgentState::Suspended;
    next
}

pub exec fn resume_execution(state: KernelState) -> (result: Result<KernelState, SyscallError>)
    ensures
        result.is_Ok() ==> {
            &&& runtime_state_is_valid(result.unwrap())
            &&& result.unwrap().agent_state == AgentState::Running
            &&& result.unwrap().cursor == result.unwrap().journal@.len()
        },
{
    if state.cursor > state.journal.len() {
        return Err(SyscallError::InvalidState);
    }
    if state.agent_state != AgentState::Suspended {
        return Err(SyscallError::InvalidState);
    }

    let mut next = state;
    next.agent_state = AgentState::Running;
    next.cursor = next.journal.len();
    Ok(next)
}

pub exec fn required_capability(effect: Effect) -> (capability: Capability)
    ensures capability@ == match effect@ {
        spec::Effect::EffectA => spec::Capability::CapA,
        spec::Effect::EffectB => spec::Capability::CapB,
    },
{
    match effect {
        Effect::EffectA => Capability::CapA,
        Effect::EffectB => Capability::CapB,
    }
}

} // verus!
