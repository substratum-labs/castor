//! C-01 DurableStorage refinement tests for T-288-B.
//!
//! Refinement map from the frozen L4 contract to the Rust target surface:
//! - `RegionPersisted` -> `EnsureRegionOutcome::Success` or equal-content replay;
//! - `EntryPersisted` -> `AppendConditionalOutcome::EntryPersisted`;
//! - stable-entry replay -> `AlreadyPersistedSameEntry` plus `read_entry` recovery;
//! - stale authority/base projection -> `RejectedPrecondition` without a partial entry;
//! - an unpersisted Region dependency -> `RejectedMissingOrUnpersistedRegion`;
//! - immutable Region identity/content disagreement -> `RejectedIdentityConflict`;
//! - unavailable persistence -> `UnavailableBeforeAck`, never durable success.
//!
//! A fresh per-Agent journal has no current projection. Its first conditional
//! append establishes the authority tuple supplied by that request. A persisted
//! `FenceRevoked { generation }` advances the current Agent generation, so a
//! later request that still expects the prior generation is stale.
//!
//! These tests deliberately exercise `PreImplementationDurableStorage`. They
//! compile in Phase 2 and remain red until the Phase-3 D1 implementation exists.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, DurabilityProfile,
    DurableStorage, EnsureRegionOutcome, PreImplementationDurableStorage, RegionPersisted,
};

const AGENT_ID: &str = "agent-c01";
const REGION_REF: &str = "region://agent-c01/action-spec";
const REGION_DIGEST: &str = "sha256:region-action-spec-v1";
const BASE_PROJECTION: &str = "sha256:projection-7";

fn attempt_armed_request(entry_id: u64) -> AppendConditionalRequest {
    AppendConditionalRequest {
        agent_id: AGENT_ID.to_string(),
        entry_id,
        expected_core_epoch: 3,
        expected_agent_generation: Some(7),
        expected_turn_id: Some(19),
        expected_lease_epoch: Some(4),
        expected_base_projection_digest: Some(BASE_PROJECTION.to_string()),
        entry: CoreEntry::AttemptArmed {
            action_id: "action-pay-42".to_string(),
            attempt_id: 1,
            request_digest: "sha256:payment-request".to_string(),
        },
        region_refs: vec![REGION_REF.to_string()],
    }
}

fn region_free_fence_request(entry_id: u64) -> AppendConditionalRequest {
    AppendConditionalRequest {
        agent_id: AGENT_ID.to_string(),
        entry_id,
        expected_core_epoch: 3,
        expected_agent_generation: Some(7),
        expected_turn_id: None,
        expected_lease_epoch: None,
        expected_base_projection_digest: Some(BASE_PROJECTION.to_string()),
        entry: CoreEntry::FenceRevoked { generation: 8 },
        region_refs: vec![],
    }
}

#[test]
fn region_persistence_refines_region_persisted_at_d1() {
    let mut storage = PreImplementationDurableStorage::new();

    let outcome = storage.ensure_region(REGION_REF, REGION_DIGEST, DurabilityProfile::D1);

    assert_eq!(
        outcome,
        EnsureRegionOutcome::Success(RegionPersisted {
            region_ref: REGION_REF.to_string(),
            content_digest: REGION_DIGEST.to_string(),
            profile: DurabilityProfile::D1,
        })
    );
}

#[test]
fn equal_region_replay_refines_idempotent_region_success() {
    let mut storage = PreImplementationDurableStorage::new();
    let persisted = RegionPersisted {
        region_ref: REGION_REF.to_string(),
        content_digest: REGION_DIGEST.to_string(),
        profile: DurabilityProfile::D1,
    };

    assert_eq!(
        storage.ensure_region(REGION_REF, REGION_DIGEST, DurabilityProfile::D1),
        EnsureRegionOutcome::Success(persisted.clone())
    );
    assert_eq!(
        storage.ensure_region(REGION_REF, REGION_DIGEST, DurabilityProfile::D1),
        EnsureRegionOutcome::AlreadyPersistedSameContent(persisted)
    );
}

#[test]
fn conflicting_region_digest_refines_immutable_identity_rejection() {
    let mut storage = PreImplementationDurableStorage::new();

    assert!(matches!(
        storage.ensure_region(REGION_REF, REGION_DIGEST, DurabilityProfile::D1),
        EnsureRegionOutcome::Success(_)
    ));
    assert_eq!(
        storage.ensure_region(
            REGION_REF,
            "sha256:different-content",
            DurabilityProfile::D1,
        ),
        EnsureRegionOutcome::RejectedIdentityConflict
    );
}

#[test]
fn conditional_append_refines_entry_persisted_for_current_authority() {
    let mut storage = PreImplementationDurableStorage::new();
    let request = region_free_fence_request(300);

    let proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("current conditional append must persist, got {other:?}"),
    };

    assert_eq!(proof.agent_id, request.agent_id);
    assert_eq!(proof.entry_id, request.entry_id);
    assert_eq!(proof.entry_kind, "FenceRevoked");
    assert!(!proof.entry_digest.is_empty());
    assert_eq!(proof.durability_profile, DurabilityProfile::D1);
    assert!(proof.referenced_region_digests.is_empty());
    assert_eq!(
        proof.expected_projection_digest,
        request.expected_base_projection_digest.unwrap()
    );
}

#[test]
fn persisted_fence_rejects_stale_authority_without_erasing_original_entry() {
    let mut storage = PreImplementationDurableStorage::new();
    let fence_request = region_free_fence_request(400);

    let fence_proof = match storage.append_conditional(fence_request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("current fence must persist before testing stale authority, got {other:?}"),
    };

    let mut stale_request = region_free_fence_request(401);
    stale_request.entry = CoreEntry::FenceRevoked { generation: 9 };

    assert!(matches!(
        storage.append_conditional(stale_request.clone()),
        AppendConditionalOutcome::RejectedPrecondition { .. }
    ));
    assert_eq!(
        storage.read_entry(&stale_request.agent_id, stale_request.entry_id),
        None
    );
    assert_eq!(
        storage.read_entry(&fence_request.agent_id, fence_request.entry_id),
        Some(fence_proof.clone())
    );
    assert_eq!(
        storage.append_conditional(fence_request),
        AppendConditionalOutcome::AlreadyPersistedSameEntry(fence_proof)
    );
}

#[test]
fn region_backed_append_proof_refines_region_before_entry_ordering() {
    let mut storage = PreImplementationDurableStorage::new();
    let request = attempt_armed_request(303);

    assert!(matches!(
        storage.ensure_region(REGION_REF, REGION_DIGEST, DurabilityProfile::D1),
        EnsureRegionOutcome::Success(_)
    ));

    let proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("entry with a persisted Region must append, got {other:?}"),
    };

    assert_eq!(proof.agent_id, request.agent_id);
    assert_eq!(proof.entry_id, request.entry_id);
    assert_eq!(proof.entry_kind, "AttemptArmed");
    assert_eq!(
        proof.referenced_region_digests,
        vec![REGION_DIGEST.to_string()]
    );
}

#[test]
fn same_entry_retry_and_read_refine_lost_ack_recovery() {
    let mut storage = PreImplementationDurableStorage::new();
    let request = region_free_fence_request(301);

    let first_proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("first append must persist before its ack is lost, got {other:?}"),
    };

    assert_eq!(
        storage.append_conditional(request.clone()),
        AppendConditionalOutcome::AlreadyPersistedSameEntry(first_proof.clone())
    );
    assert_eq!(
        storage.read_entry(&request.agent_id, request.entry_id),
        Some(first_proof.clone())
    );

    let mut conflicting_request = request.clone();
    conflicting_request.entry = CoreEntry::TurnCommitted { turn_id: 99 };

    assert!(matches!(
        storage.append_conditional(conflicting_request),
        AppendConditionalOutcome::IntegrityFault
            | AppendConditionalOutcome::RejectedPrecondition { .. }
    ));
    assert_eq!(
        storage.read_entry(&request.agent_id, request.entry_id),
        Some(first_proof)
    );
}

#[test]
fn unpersisted_region_refines_append_rejection_without_partial_entry() {
    let mut storage = PreImplementationDurableStorage::new();
    let request = attempt_armed_request(302);

    assert_eq!(
        storage.append_conditional(request.clone()),
        AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion
    );
    assert_eq!(
        storage.read_entry(&request.agent_id, request.entry_id),
        None
    );

    assert!(matches!(
        storage.ensure_region(REGION_REF, REGION_DIGEST, DurabilityProfile::D1),
        EnsureRegionOutcome::Success(_)
    ));

    let proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!(
            "missing-Region rejection must not consume the entry identity; retry got {other:?}"
        ),
    };
    assert_eq!(
        proof.referenced_region_digests,
        vec![REGION_DIGEST.to_string()]
    );
}
