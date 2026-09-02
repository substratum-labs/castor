//! RED refinement tests for the EPIC-22 C-02 D1 execution-authority slice.
//!
//! Each test names a concrete protocol break: accepting a second lease, a
//! pre-fence advance, a tampered authority tuple, or a retry that mistakes a
//! lost acknowledgement for absence.  These are behavioral tests for the
//! future Core boundary; they do not assert source layout, transport details,
//! or an Agent-side mock.

use castor_kernel::c01_storage::{D1DurableStorage, DurableStorage};
use castor_kernel::c02_execution::{
    AdvanceTurnRequest, AuthorityTuple, D1ExecutionAuthority, ExecutionAuthority, ExecutionOutcome,
};
use tempfile::TempDir;

const AGENT_ID: &str = "agent-c02";
const INCARNATION_ID: &str = "incarnation-i7";
const CORE_EPOCH: u64 = 3;
const GENERATION: u64 = 4;
const TURN_ID: u64 = 17;
const LEASE_EPOCH: u64 = 9;
const BASE_DIGEST: &str = "sha256:base-projection-0";

fn authority() -> (TempDir, D1ExecutionAuthority) {
    let root = tempfile::tempdir().expect("temporary D1 root");
    let storage = D1DurableStorage::open(root.path()).expect("open D1 storage");
    let authority = D1ExecutionAuthority::for_ready_turn(
        storage,
        AGENT_ID,
        CORE_EPOCH,
        GENERATION,
        TURN_ID,
        LEASE_EPOCH,
        BASE_DIGEST,
    );
    (root, authority)
}

fn current_tuple() -> AuthorityTuple {
    AuthorityTuple {
        agent_id: AGENT_ID.to_string(),
        core_epoch: CORE_EPOCH,
        agent_generation: GENERATION,
        incarnation_id: INCARNATION_ID.to_string(),
        turn_id: TURN_ID,
        lease_epoch: LEASE_EPOCH,
        base_projection_digest: BASE_DIGEST.to_string(),
    }
}

fn bind_current(authority: &mut D1ExecutionAuthority) {
    assert_eq!(
        authority.bind_incarnation(INCARNATION_ID, GENERATION),
        ExecutionOutcome::IncarnationBound {
            incarnation_id: INCARNATION_ID.to_string(),
            agent_generation: GENERATION,
        }
    );
}

fn grant_current(authority: &mut D1ExecutionAuthority) {
    assert_eq!(
        authority.grant_execution_lease(current_tuple(), 100),
        ExecutionOutcome::LeaseGranted {
            persisted_entry_id: 100,
            permits_direct_durable_write: false,
            permits_direct_effect_dispatch: false,
        }
    );
}

#[test]
fn test_c02_normal_bound_lease_to_advance() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    assert_eq!(
        authority.advance_turn(AdvanceTurnRequest {
            tuple: current_tuple(),
            entry_id: 101,
            successor_projection_digest: "sha256:base-projection-1".to_string(),
            action_manifest_digest: Some("sha256:action-intent".to_string()),
        }),
        ExecutionOutcome::TurnCommitted {
            persisted_entry_id: 101,
            successor_projection_digest: "sha256:base-projection-1".to_string(),
        }
    );
    assert_eq!(
        authority
            .storage()
            .read_entry(AGENT_ID, 100)
            .map(|proof| proof.entry_kind),
        Some("LeaseGranted".to_string())
    );
    assert_eq!(
        authority
            .storage()
            .read_entry(AGENT_ID, 101)
            .map(|proof| proof.entry_kind),
        Some("TurnCommitted".to_string())
    );
}

#[test]
fn test_c02_at_most_one_current_lease() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    assert_eq!(
        authority.grant_execution_lease(current_tuple(), 102),
        ExecutionOutcome::RejectedPrecondition
    );
}

#[test]
fn test_c02_durable_fence_vs_stale_advance() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    assert_eq!(
        authority.revoke_or_fence_execution(CORE_EPOCH, GENERATION, GENERATION + 1, 200),
        ExecutionOutcome::GenerationFenced {
            persisted_entry_id: 200,
            agent_generation: GENERATION + 1,
        }
    );
    assert_eq!(
        authority.advance_turn(AdvanceTurnRequest {
            tuple: current_tuple(),
            entry_id: 201,
            successor_projection_digest: "sha256:base-projection-1".to_string(),
            action_manifest_digest: None,
        }),
        ExecutionOutcome::RejectedStaleAuthority {
            current_generation: GENERATION + 1,
            current_lease_epoch: None,
        }
    );
}

#[test]
fn test_c02_no_direct_agent_durable_or_effect_authority() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    assert_eq!(
        authority.current_lease_permissions(),
        Some((false, false)),
        "a granted lease may request AdvanceTurn through Core only"
    );
}

#[test]
fn test_c02_tuple_tampering_rejection() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    let mut variants = Vec::new();
    let mut tuple = current_tuple();
    tuple.core_epoch += 1;
    variants.push(tuple);
    let mut tuple = current_tuple();
    tuple.agent_generation += 1;
    variants.push(tuple);
    let mut tuple = current_tuple();
    tuple.incarnation_id = "incarnation-i8".to_string();
    variants.push(tuple);
    let mut tuple = current_tuple();
    tuple.turn_id += 1;
    variants.push(tuple);
    let mut tuple = current_tuple();
    tuple.lease_epoch += 1;
    variants.push(tuple);
    let mut tuple = current_tuple();
    tuple.base_projection_digest = "sha256:tampered".to_string();
    variants.push(tuple);

    for tuple in variants {
        assert!(matches!(
            authority.advance_turn(AdvanceTurnRequest {
                tuple,
                entry_id: 300,
                successor_projection_digest: "sha256:base-projection-1".to_string(),
                action_manifest_digest: None,
            }),
            ExecutionOutcome::RejectedStaleAuthority { .. }
        ));
    }
}

#[test]
fn test_c02_stale_carrier_replay_without_rebind_fails() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    assert_eq!(
        authority.revoke_or_fence_execution(CORE_EPOCH, GENERATION, GENERATION + 1, 400),
        ExecutionOutcome::GenerationFenced {
            persisted_entry_id: 400,
            agent_generation: GENERATION + 1,
        }
    );
    assert!(matches!(
        authority.grant_execution_lease(current_tuple(), 401),
        ExecutionOutcome::RejectedStaleAuthority { .. }
    ));
}

#[test]
fn test_c02_lost_ack_retry_idempotency() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);

    assert_eq!(
        authority.grant_execution_lease(current_tuple(), 500),
        ExecutionOutcome::LeaseGranted {
            persisted_entry_id: 500,
            permits_direct_durable_write: false,
            permits_direct_effect_dispatch: false,
        }
    );
    assert_eq!(
        authority.grant_execution_lease(current_tuple(), 500),
        ExecutionOutcome::LeaseGranted {
            persisted_entry_id: 500,
            permits_direct_durable_write: false,
            permits_direct_effect_dispatch: false,
        },
        "a lost acknowledgement must recover the same entry rather than infer absence"
    );
}

#[test]
fn test_c02_fence_prevents_recovery_of_a_superseded_lease_entry() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    assert_eq!(
        authority.revoke_or_fence_execution(CORE_EPOCH, GENERATION, GENERATION + 1, 600),
        ExecutionOutcome::GenerationFenced {
            persisted_entry_id: 600,
            agent_generation: GENERATION + 1,
        }
    );

    assert_eq!(
        authority.grant_execution_lease(current_tuple(), 100),
        ExecutionOutcome::RejectedStaleAuthority {
            current_generation: GENERATION + 1,
            current_lease_epoch: None,
        },
        "a stable lease entry from before a durable fence must never restore the old lease"
    );
    assert_eq!(authority.current_lease_permissions(), None);
}

#[test]
fn test_c02_fence_prevents_recovery_of_a_superseded_turn_entry() {
    let (_root, mut authority) = authority();
    bind_current(&mut authority);
    grant_current(&mut authority);

    let committed = AdvanceTurnRequest {
        tuple: current_tuple(),
        entry_id: 601,
        successor_projection_digest: "sha256:base-projection-1".to_string(),
        action_manifest_digest: None,
    };
    assert!(matches!(
        authority.advance_turn(committed.clone()),
        ExecutionOutcome::TurnCommitted { .. }
    ));
    assert_eq!(
        authority.revoke_or_fence_execution(CORE_EPOCH, GENERATION, GENERATION + 1, 602),
        ExecutionOutcome::GenerationFenced {
            persisted_entry_id: 602,
            agent_generation: GENERATION + 1,
        }
    );

    assert_eq!(
        authority.advance_turn(committed),
        ExecutionOutcome::RejectedStaleAuthority {
            current_generation: GENERATION + 1,
            current_lease_epoch: None,
        },
        "a stable turn entry from before a durable fence must never restore its old projection"
    );
}
