use castor_kernel::runtime::{
    grant_capability, revoke_capability, syscall_commit, syscall_propose, AgentState, Capability,
    Effect, KernelState, SyscallError,
};

#[test]
fn public_runtime_api_commits_an_authorized_pending_syscall() {
    let state = grant_capability(KernelState::new(), Capability::CapA);
    let pending = syscall_propose(state, Effect::EffectA).unwrap();
    let committed = syscall_commit(pending, Effect::EffectA).unwrap();

    assert_eq!(committed.agent_state, AgentState::Running);
    assert_eq!(committed.cursor, 2);
}

#[test]
fn revocation_between_proposal_and_commit_blocks_the_syscall() {
    let state = grant_capability(KernelState::new(), Capability::CapA);
    let pending = syscall_propose(state, Effect::EffectA).unwrap();
    let revoked = revoke_capability(pending, Capability::CapA);

    assert_eq!(
        syscall_commit(revoked, Effect::EffectA),
        Err(SyscallError::Unauthorized),
    );
}
