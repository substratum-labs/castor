//! RED refinement tests for the EPIC-23 C-03 D1 interaction-continuation slice.
//!
//! Each test names a protocol break at the Core boundary. They use no provider
//! mock: Phase 3 must refine these observable outcomes onto C-01/C-02.

use castor_kernel::c03_interaction::{
    D1InteractionAuthority, InteractionContinuation, InteractionIdentity, InteractionOutcome,
    InteractionRequest, InteractionResultReport,
};
use tempfile::TempDir;

const AGENT_ID: &str = "agent-c03";
const TURN_ID: u64 = 27;
const LEASE_EPOCH: u64 = 11;
const FRESH_LEASE_EPOCH: u64 = 12;
const INTERACTION_ID: &str = "interaction-iris";
const REQUEST_DIGEST: &str = "sha256:request-iris";
const RESULT_REGION: &str = "region:result-iris";
const RESULT_BYTES: &[u8] = b"observation bytes";
const RESULT_DIGEST: &str =
    "sha256:6570871d884fe4de6143077bbac541c1f708753b567c32d60b78c206cc7831cf";
const SERVICE_ID: &str = "service:observer";

fn interaction() -> (TempDir, D1InteractionAuthority) {
    let root = tempfile::tempdir().expect("temporary C-03 root");
    (
        root,
        D1InteractionAuthority::for_ready_turn(
            AGENT_ID,
            TURN_ID,
            LEASE_EPOCH,
            "sha256:base-projection-0",
        )
        .expect("temporary C-03 authority"),
    )
}

fn identity() -> InteractionIdentity {
    InteractionIdentity {
        agent_id: AGENT_ID.to_string(),
        turn_id: TURN_ID,
        interaction_id: INTERACTION_ID.to_string(),
        request_digest: REQUEST_DIGEST.to_string(),
        service_id: SERVICE_ID.to_string(),
    }
}

fn request(entry_id: u64) -> InteractionRequest {
    InteractionRequest {
        identity: identity(),
        lease_epoch: LEASE_EPOCH,
        entry_id,
    }
}

fn report(entry_id: u64) -> InteractionResultReport {
    InteractionResultReport {
        identity: identity(),
        region_id: RESULT_REGION.to_string(),
        result_digest: RESULT_DIGEST.to_string(),
        disposition: "completed".to_string(),
        entry_id,
    }
}

fn request_and_persist(authority: &mut D1InteractionAuthority) {
    assert_eq!(
        authority.request_interaction(request(100)),
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: INTERACTION_ID.to_string(),
            persisted_entry_id: 100,
        }
    );
    assert_eq!(
        authority.persist_interaction_result_region(
            AGENT_ID,
            INTERACTION_ID,
            RESULT_REGION,
            RESULT_DIGEST,
            RESULT_BYTES,
        ),
        InteractionOutcome::ResultRegionStored {
            region_id: RESULT_REGION.to_string(),
            result_digest: RESULT_DIGEST.to_string(),
        }
    );
}

fn bind(authority: &mut D1InteractionAuthority) {
    request_and_persist(authority);
    assert_eq!(
        authority.report_interaction_outcome(report(101)),
        InteractionOutcome::Bound {
            persisted_entry_id: 101,
            region_id: RESULT_REGION.to_string(),
        }
    );
}

#[test]
fn test_c03_normal_request_region_bind_flow() {
    let (_root, mut authority) = interaction();
    bind(&mut authority);
    assert_eq!(
        authority.grant_fresh_execution_lease(FRESH_LEASE_EPOCH, 102),
        InteractionOutcome::FreshLeaseGranted {
            persisted_entry_id: 102,
            lease_epoch: FRESH_LEASE_EPOCH,
        }
    );
    assert_eq!(
        authority.consume_interaction(INTERACTION_ID, FRESH_LEASE_EPOCH),
        InteractionOutcome::InteractionConsumed {
            interaction_id: INTERACTION_ID.to_string(),
            region_id: RESULT_REGION.to_string(),
        }
    );
}

#[test]
fn test_c03_request_interaction_revokes_lease_and_awaits() {
    let (_root, mut authority) = interaction();
    assert_eq!(
        authority.request_interaction(request(110)),
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: INTERACTION_ID.to_string(),
            persisted_entry_id: 110,
        }
    );
    assert_eq!(authority.active_lease_epoch(), None);
    assert!(authority.is_awaiting_interaction());
}

#[test]
fn test_c03_lost_ack_request_interaction_is_idempotent() {
    let (_root, mut authority) = interaction();
    assert_eq!(
        authority.request_interaction(request(120)),
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: INTERACTION_ID.to_string(),
            persisted_entry_id: 120,
        }
    );
    assert_eq!(
        authority.request_interaction(request(120)),
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: INTERACTION_ID.to_string(),
            persisted_entry_id: 120,
        },
        "a lost acknowledgement must recover the existing request, not append another"
    );
    assert_eq!(authority.journal_entries(), 1);
}

#[test]
fn test_c03_duplicate_outcome_report_idempotence() {
    let (_root, mut authority) = interaction();
    bind(&mut authority);
    assert_eq!(
        authority.report_interaction_outcome(report(101)),
        InteractionOutcome::AlreadyBoundSameOutcome {
            persisted_entry_id: 101,
        }
    );
    assert_eq!(authority.journal_entries(), 2);
}

#[test]
fn test_c03_late_report_on_closed_turn_rejected() {
    let (_root, mut authority) = interaction();
    request_and_persist(&mut authority);
    assert_eq!(
        authority.close_or_fence_turn(130),
        InteractionOutcome::TurnClosedOrFenced {
            persisted_entry_id: 130,
        }
    );
    assert_eq!(
        authority.report_interaction_outcome(report(131)),
        InteractionOutcome::RejectedLateOrClosedTurn
    );
    assert_eq!(authority.bound_region(INTERACTION_ID), None);
}

#[test]
fn test_c03_conflicting_report_does_not_unbind_existing_bound_region() {
    let (_root, mut authority) = interaction();
    bind(&mut authority);
    let mut conflicting = report(141);
    conflicting.region_id = "region:conflict".to_string();
    conflicting.result_digest = "sha256:conflict".to_string();
    assert_eq!(
        authority.report_interaction_outcome(conflicting),
        InteractionOutcome::RejectedConflictingOutcome {
            persisted_entry_id: 141,
        }
    );
    assert_eq!(
        authority.bound_region(INTERACTION_ID),
        Some(RESULT_REGION.to_string())
    );
}

#[test]
fn test_c03_turn_aborted_or_fenced_while_interaction_in_flight() {
    let (_root, mut authority) = interaction();
    request_and_persist(&mut authority);
    assert_eq!(
        authority.close_or_fence_turn(150),
        InteractionOutcome::TurnClosedOrFenced {
            persisted_entry_id: 150,
        }
    );
    assert_eq!(
        authority.report_interaction_outcome(report(151)),
        InteractionOutcome::RejectedLateOrClosedTurn
    );
}

#[test]
fn test_c03_stale_lease_consumption_rejected() {
    let (_root, mut authority) = interaction();
    bind(&mut authority);
    assert_eq!(
        authority.consume_interaction(INTERACTION_ID, LEASE_EPOCH),
        InteractionOutcome::RejectedStaleAuthority
    );
}

#[test]
fn test_c03_unpersisted_region_binding_rejected() {
    let (_root, mut authority) = interaction();
    assert_eq!(
        authority.request_interaction(request(160)),
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: INTERACTION_ID.to_string(),
            persisted_entry_id: 160,
        }
    );
    assert_eq!(
        authority.report_interaction_outcome(report(161)),
        InteractionOutcome::IntegrityOrProtocolFault
    );
}

#[test]
fn test_c03_result_digest_mismatch_is_rejected_before_region_persistence() {
    let (_root, mut authority) = interaction();
    assert_eq!(
        authority.request_interaction(request(170)),
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: INTERACTION_ID.to_string(),
            persisted_entry_id: 170,
        }
    );
    assert_eq!(
        authority.persist_interaction_result_region(
            AGENT_ID,
            INTERACTION_ID,
            RESULT_REGION,
            "sha256:caller-lie",
            RESULT_BYTES,
        ),
        InteractionOutcome::IntegrityOrProtocolFault,
        "the caller-provided digest must attest to the exact Region bytes"
    );
    assert_eq!(
        authority.persist_interaction_result_region(
            AGENT_ID,
            INTERACTION_ID,
            RESULT_REGION,
            RESULT_DIGEST,
            RESULT_BYTES,
        ),
        InteractionOutcome::ResultRegionStored {
            region_id: RESULT_REGION.to_string(),
            result_digest: RESULT_DIGEST.to_string(),
        },
        "a rejected digest mismatch must not advance the interaction projection"
    );
}

#[test]
fn test_c03_fresh_lease_epoch_cannot_be_reused_after_a_later_interaction() {
    let (_root, mut authority) = interaction();
    bind(&mut authority);
    assert_eq!(
        authority.grant_fresh_execution_lease(FRESH_LEASE_EPOCH, 180),
        InteractionOutcome::FreshLeaseGranted {
            persisted_entry_id: 180,
            lease_epoch: FRESH_LEASE_EPOCH,
        }
    );

    let second_identity = InteractionIdentity {
        interaction_id: "interaction-lotus".to_string(),
        ..identity()
    };
    assert_eq!(
        authority.request_interaction(InteractionRequest {
            identity: second_identity.clone(),
            lease_epoch: FRESH_LEASE_EPOCH,
            entry_id: 181,
        }),
        InteractionOutcome::InteractionRequestedAck {
            interaction_id: second_identity.interaction_id.clone(),
            persisted_entry_id: 181,
        }
    );
    assert_eq!(
        authority.persist_interaction_result_region(
            AGENT_ID,
            &second_identity.interaction_id,
            "region:result-lotus",
            RESULT_DIGEST,
            RESULT_BYTES,
        ),
        InteractionOutcome::ResultRegionStored {
            region_id: "region:result-lotus".to_string(),
            result_digest: RESULT_DIGEST.to_string(),
        }
    );
    assert_eq!(
        authority.report_interaction_outcome(InteractionResultReport {
            identity: second_identity,
            region_id: "region:result-lotus".to_string(),
            result_digest: RESULT_DIGEST.to_string(),
            disposition: "completed".to_string(),
            entry_id: 182,
        }),
        InteractionOutcome::Bound {
            persisted_entry_id: 182,
            region_id: "region:result-lotus".to_string(),
        }
    );
    assert_eq!(
        authority.grant_fresh_execution_lease(FRESH_LEASE_EPOCH, 183),
        InteractionOutcome::RejectedStaleAuthority,
        "a stale carrier must not regain the same lease epoch after a later interaction"
    );
    assert_eq!(
        authority.grant_fresh_execution_lease(FRESH_LEASE_EPOCH + 1, 184),
        InteractionOutcome::FreshLeaseGranted {
            persisted_entry_id: 184,
            lease_epoch: FRESH_LEASE_EPOCH + 1,
        }
    );
}
