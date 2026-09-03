//! RED hostile-contract tests for governed turn composition.
//!
//! The charter freezes the observable C-06 composition boundary before an
//! implementation exists. Every fixture digest attests to concrete bytes.

use castor_kernel::governed_turn_composition::{
    CompletionReport, D1GovernedTurnComposition, GovernedTurnComposition, GovernedTurnOutcome,
    StartTurnRequest, TurnAuthority, TurnDisposition,
};
use sha2::{Digest, Sha256};

const AGENT_ID: &str = "agent-governed-turn";
const INCARNATION_ID: &str = "incarnation:governed-turn";
const TURN_ID: u64 = 41;
const CORE_EPOCH: u64 = 3;
const GENERATION: u64 = 9;
const LEASE_EPOCH: u64 = 17;

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn authority() -> TurnAuthority {
    TurnAuthority {
        agent_id: AGENT_ID.into(),
        core_epoch: CORE_EPOCH,
        agent_generation: GENERATION,
        incarnation_id: INCARNATION_ID.into(),
        turn_id: TURN_ID,
        lease_epoch: LEASE_EPOCH,
        base_projection_digest: digest(b"base projection v1"),
    }
}

fn start(entry_id: u64) -> StartTurnRequest {
    StartTurnRequest {
        authority: authority(),
        input_region_id: "region://input/41".into(),
        input_digest: digest(b"user: transfer 42 credits"),
        policy_digest: digest(b"policy: governed-turn-v1"),
        entry_id,
    }
}

fn completion(entry_id: u64) -> CompletionReport {
    CompletionReport {
        authority: authority(),
        output_region_id: "region://output/41".into(),
        output_digest: digest(b"assistant: transfer requires admission"),
        interaction_digest: digest(b"interaction lineage: none"),
        settlement_digest: digest(b"settlement lineage: none"),
        successor_projection_digest: digest(b"base projection v2"),
        disposition: TurnDisposition::Completed,
        entry_id,
    }
}

fn harness() -> D1GovernedTurnComposition {
    D1GovernedTurnComposition::for_test(AGENT_ID, CORE_EPOCH, GENERATION)
}

fn start_turn(core: &mut D1GovernedTurnComposition, entry_id: u64) {
    assert_eq!(
        core.start_turn(start(entry_id)),
        GovernedTurnOutcome::TurnStarted { entry_id }
    );
}

#[test]
fn test_c06_normal_governed_turn_composes_to_a_committed_successor() {
    let mut core = harness();
    start_turn(&mut core, 10);
    assert_eq!(
        core.complete_turn(completion(11)),
        GovernedTurnOutcome::TurnCommitted {
            entry_id: 11,
            successor_projection_digest: digest(b"base projection v2"),
        }
    );
}

#[test]
fn test_c06_start_requires_the_bound_agent_identity() {
    let mut core = harness();
    let mut request = start(20);
    request.authority.agent_id = "agent-other".into();
    assert_eq!(
        core.start_turn(request),
        GovernedTurnOutcome::RejectedStaleAuthority {
            current_generation: GENERATION,
            current_lease_epoch: None,
        }
    );
}

#[test]
fn test_c06_start_requires_the_expected_core_epoch() {
    let mut core = harness();
    let mut request = start(30);
    request.authority.core_epoch += 1;
    assert_eq!(
        core.start_turn(request),
        GovernedTurnOutcome::RejectedStaleAuthority {
            current_generation: GENERATION,
            current_lease_epoch: None,
        }
    );
}

#[test]
fn test_c06_start_requires_the_bound_incarnation() {
    let mut core = harness();
    let mut request = start(40);
    request.authority.incarnation_id = "incarnation:stale".into();
    assert_eq!(
        core.start_turn(request),
        GovernedTurnOutcome::RejectedStaleAuthority {
            current_generation: GENERATION,
            current_lease_epoch: None,
        }
    );
}

#[test]
fn test_c06_start_requires_the_current_base_projection_digest() {
    let mut core = harness();
    let mut request = start(50);
    request.authority.base_projection_digest = digest(b"stale base projection");
    assert_eq!(
        core.start_turn(request),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn test_c06_lost_start_ack_recovers_the_same_persisted_turn() {
    let mut core = harness();
    start_turn(&mut core, 60);
    assert_eq!(
        core.start_turn(start(60)),
        GovernedTurnOutcome::TurnStarted { entry_id: 60 }
    );
}

#[test]
fn test_c06_second_live_turn_is_rejected_until_the_first_is_resolved() {
    let mut core = harness();
    start_turn(&mut core, 70);
    let mut overlapping = start(71);
    overlapping.authority.turn_id += 1;
    assert_eq!(
        core.start_turn(overlapping),
        GovernedTurnOutcome::RejectedCurrentState
    );
}

#[test]
fn test_c06_completion_requires_the_active_lease_epoch() {
    let mut core = harness();
    start_turn(&mut core, 80);
    let mut report = completion(81);
    report.authority.lease_epoch += 1;
    assert_eq!(
        core.complete_turn(report),
        GovernedTurnOutcome::RejectedStaleAuthority {
            current_generation: GENERATION,
            current_lease_epoch: Some(LEASE_EPOCH),
        }
    );
}

#[test]
fn test_c06_completion_rejects_an_output_digest_that_does_not_attest_to_output() {
    let mut core = harness();
    start_turn(&mut core, 100);
    let mut report = completion(101);
    report.output_digest = digest(b"caller supplied a different output");
    assert_eq!(
        core.complete_turn(report),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
}

#[test]
fn test_c06_completion_rejects_an_unrecognized_interaction_lineage() {
    let mut core = harness();
    start_turn(&mut core, 110);
    let mut report = completion(111);
    report.interaction_digest = digest(b"interaction lineage: unpersisted");
    assert_eq!(
        core.complete_turn(report),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
}

#[test]
fn test_c06_completion_rejects_an_unrecognized_settlement_lineage() {
    let mut core = harness();
    start_turn(&mut core, 120);
    let mut report = completion(121);
    report.settlement_digest = digest(b"settlement lineage: unpersisted");
    assert_eq!(
        core.complete_turn(report),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
}

#[test]
fn test_c06_completion_requires_a_successor_projection_digest() {
    let mut core = harness();
    start_turn(&mut core, 130);
    let mut report = completion(131);
    report.successor_projection_digest.clear();
    assert_eq!(
        core.complete_turn(report),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
}

#[test]
fn test_c06_lost_commit_ack_recovers_the_same_persisted_completion() {
    let mut core = harness();
    start_turn(&mut core, 140);
    assert!(matches!(
        core.complete_turn(completion(141)),
        GovernedTurnOutcome::TurnCommitted { .. }
    ));
    assert_eq!(
        core.complete_turn(completion(141)),
        GovernedTurnOutcome::AlreadyCommittedSameCompletion { entry_id: 141 }
    );
}

#[test]
fn test_c06_conflicting_duplicate_completion_is_quarantined() {
    let mut core = harness();
    start_turn(&mut core, 150);
    assert!(matches!(
        core.complete_turn(completion(151)),
        GovernedTurnOutcome::TurnCommitted { .. }
    ));
    let mut conflict = completion(152);
    conflict.output_digest = digest(b"conflicting output");
    assert_eq!(
        core.complete_turn(conflict),
        GovernedTurnOutcome::ConflictingCompletionQuarantined { entry_id: 152 }
    );
}

#[test]
fn test_c06_abort_revokes_the_live_lease_and_records_the_fence() {
    let mut core = harness();
    start_turn(&mut core, 160);
    assert_eq!(
        core.abort_turn(authority(), 161),
        GovernedTurnOutcome::TurnAborted { entry_id: 161 }
    );
    assert_eq!(core.active_lease_epoch(), None);
}

#[test]
fn test_c06_completion_after_abort_is_rejected() {
    let mut core = harness();
    start_turn(&mut core, 170);
    assert!(matches!(
        core.abort_turn(authority(), 171),
        GovernedTurnOutcome::TurnAborted { .. }
    ));
    assert_eq!(
        core.complete_turn(completion(172)),
        GovernedTurnOutcome::RejectedCurrentState
    );
}

#[test]
fn test_c06_generation_fence_rejects_an_in_flight_completion() {
    let mut core = harness();
    start_turn(&mut core, 180);
    core.fence_generation(GENERATION + 1);
    assert_eq!(
        core.complete_turn(completion(181)),
        GovernedTurnOutcome::RejectedStaleAuthority {
            current_generation: GENERATION + 1,
            current_lease_epoch: None,
        }
    );
}

#[test]
fn test_c06_recovery_preserves_an_in_flight_turn_as_unresolved() {
    let mut core = harness();
    start_turn(&mut core, 190);
    core.reconstruct_after_crash();
    assert_eq!(
        core.turn_is_unresolved(TURN_ID),
        true,
        "recovery must not silently retry or commit an interrupted governed turn"
    );
}
