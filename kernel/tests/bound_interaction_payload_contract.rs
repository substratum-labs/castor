//! T-320-D3 least-authority contract for consuming bound Interaction payloads.

use castor_kernel::c01_storage::{D1DurableStorage, DurabilityProfile, DurableStorage};
use castor_kernel::c06_composition::{
    AdmitTurnRequest, ConsumeInteractionRequest, ConsumedInteractionPayload,
    D1GovernedTurnAuthority, GovernedTurnOutcome, InteractionOutcomeReport,
    RequestInteractionRequest,
};
use sha2::{Digest, Sha256};
use tempfile::TempDir;

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn setup_awaiting_interaction() -> (TempDir, D1GovernedTurnAuthority, Vec<u8>, String) {
    let root = tempfile::tempdir().expect("temporary D1 root");
    let mut storage = D1DurableStorage::open(root.path()).expect("open D1 storage");
    let content = b"the current bound observation".to_vec();
    let observation_digest = digest(&content);
    assert!(matches!(
        storage.ensure_region(
            "region://current-observation",
            &observation_digest,
            &content,
            DurabilityProfile::D1,
        ),
        castor_kernel::c01_storage::EnsureRegionOutcome::Success(_)
    ));
    let mut authority = D1GovernedTurnAuthority::for_test(storage);
    assert!(matches!(
        authority.admit_turn(AdmitTurnRequest {
            agent_id: "agent-1".into(),
            turn_id: 1,
            lease_epoch: 0,
            base_projection_digest: digest(b"H0"),
            cap_id: None,
        }),
        GovernedTurnOutcome::Admitted { .. }
    ));
    assert_eq!(
        authority.request_interaction(RequestInteractionRequest {
            query_operation: None,
            interaction_id: "interaction-1".into(),
            lease_epoch: 0,
            request_digest: digest(b"request"),
        }),
        GovernedTurnOutcome::InteractionRequested
    );
    (root, authority, content, observation_digest)
}

fn setup_bound_interaction() -> (TempDir, D1GovernedTurnAuthority, Vec<u8>, String) {
    let (root, mut authority, content, observation_digest) = setup_awaiting_interaction();
    assert_eq!(
        authority.report_outcome(InteractionOutcomeReport {
            interaction_id: "interaction-1".into(),
            observation_region_id: "region://current-observation".into(),
            observation_digest: observation_digest.clone(),
        }),
        GovernedTurnOutcome::InteractionBound
    );
    (root, authority, content, observation_digest)
}

#[test]
fn current_bound_interaction_returns_only_its_verified_payload() {
    let root = tempfile::tempdir().expect("temporary D1 root");
    let mut storage = D1DurableStorage::open(root.path()).expect("open D1 storage");
    let content = b"the current bound observation";
    let observation_digest = digest(content);
    assert!(matches!(
        storage.ensure_region(
            "region://current-observation",
            &observation_digest,
            content,
            DurabilityProfile::D1,
        ),
        castor_kernel::c01_storage::EnsureRegionOutcome::Success(_)
    ));

    let mut authority = D1GovernedTurnAuthority::for_test(storage);
    assert!(matches!(
        authority.admit_turn(AdmitTurnRequest {
            agent_id: "agent-1".into(),
            turn_id: 1,
            lease_epoch: 0,
            base_projection_digest: digest(b"H0"),
            cap_id: None,
        }),
        GovernedTurnOutcome::Admitted { .. }
    ));
    assert_eq!(
        authority.request_interaction(RequestInteractionRequest {
            query_operation: None,
            interaction_id: "interaction-1".into(),
            lease_epoch: 0,
            request_digest: digest(b"request"),
        }),
        GovernedTurnOutcome::InteractionRequested
    );
    assert_eq!(
        authority.report_outcome(InteractionOutcomeReport {
            interaction_id: "interaction-1".into(),
            observation_region_id: "region://current-observation".into(),
            observation_digest: observation_digest.clone(),
        }),
        GovernedTurnOutcome::InteractionBound
    );

    assert_eq!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 1,
        }),
        GovernedTurnOutcome::InteractionConsumed(ConsumedInteractionPayload {
            interaction_id: "interaction-1".into(),
            observation_region_id: "region://current-observation".into(),
            observation_digest,
            content: content.to_vec(),
            lease_epoch: 1,
        })
    );
}

#[test]
fn unknown_or_not_yet_bound_interaction_returns_no_payload_and_writes_nothing() {
    let (_root, mut authority, _content, _digest) = setup_awaiting_interaction();
    let before = authority.inspect_journal();
    assert_eq!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 1,
        }),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(authority.inspect_journal(), before);

    let (_root, mut authority, _content, _digest) = setup_bound_interaction();
    let before = authority.inspect_journal();
    assert_eq!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-not-bound".into(),
            lease_epoch: 1,
        }),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(authority.inspect_journal(), before);
}

#[test]
fn identical_lost_response_retry_returns_same_payload_without_another_append() {
    let (_root, mut authority, _content, _digest) = setup_bound_interaction();
    let request = ConsumeInteractionRequest {
        interaction_id: "interaction-1".into(),
        lease_epoch: 1,
    };
    let first = authority.consume_interaction(request.clone());
    let after_first = authority.inspect_journal();
    let retry = authority.consume_interaction(request);
    assert_eq!(retry, first);
    assert_eq!(authority.inspect_journal(), after_first);
    assert!(matches!(retry, GovernedTurnOutcome::InteractionConsumed(_)));
}

#[test]
fn different_lease_or_closed_turn_cannot_reconsume_a_binding() {
    let (_root, mut authority, _content, _digest) = setup_bound_interaction();
    assert!(matches!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 1,
        }),
        GovernedTurnOutcome::InteractionConsumed(_)
    ));
    let before = authority.inspect_journal();
    assert_eq!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 2,
        }),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(authority.inspect_journal(), before);
    assert_eq!(
        authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    assert_eq!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 1,
        }),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
}

#[test]
fn a_later_interaction_cannot_reauthorize_an_already_consumed_binding() {
    let (_root, mut authority, _content, observation_digest) = setup_bound_interaction();
    assert!(matches!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 1,
        }),
        GovernedTurnOutcome::InteractionConsumed(_)
    ));
    assert_eq!(
        authority.request_interaction(RequestInteractionRequest {
            query_operation: None,
            interaction_id: "interaction-2".into(),
            lease_epoch: 1,
            request_digest: digest(b"second request"),
        }),
        GovernedTurnOutcome::InteractionRequested
    );
    assert_eq!(
        authority.report_outcome(InteractionOutcomeReport {
            interaction_id: "interaction-2".into(),
            observation_region_id: "region://current-observation".into(),
            observation_digest,
        }),
        GovernedTurnOutcome::InteractionBound
    );
    let before = authority.inspect_journal();
    assert_eq!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 2,
        }),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(authority.inspect_journal(), before);
    assert!(matches!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-2".into(),
            lease_epoch: 2,
        }),
        GovernedTurnOutcome::InteractionConsumed(_)
    ));
}

#[test]
fn digest_mismatch_and_late_report_fail_before_any_payload_can_be_consumed() {
    let (_root, mut authority, _content, _digest) = setup_awaiting_interaction();
    let before = authority.inspect_journal();
    assert_eq!(
        authority.report_outcome(InteractionOutcomeReport {
            interaction_id: "interaction-1".into(),
            observation_region_id: "region://current-observation".into(),
            observation_digest: digest(b"wrong bytes"),
        }),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
    assert_eq!(authority.inspect_journal(), before);
    assert_eq!(
        authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    assert_eq!(
        authority.report_outcome(InteractionOutcomeReport {
            interaction_id: "interaction-1".into(),
            observation_region_id: "region://current-observation".into(),
            observation_digest: digest(b"the current bound observation"),
        }),
        GovernedTurnOutcome::RejectedLateOrClosedTurn
    );
}
