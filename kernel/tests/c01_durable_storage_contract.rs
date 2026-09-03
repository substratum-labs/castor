//! C-01 DurableStorage D1 refinement tests for T-288-C.
//!
//! Refinement map from the frozen L4 contract to the Rust target surface:
//! - `RegionPersisted` -> D1-synced bytes plus `EnsureRegionOutcome::Success`;
//! - `EntryPersisted` -> an fsynced conditional journal record;
//! - stable-entry replay -> `AlreadyPersistedSameEntry` after process reopen;
//! - stale authority/base projection -> `RejectedPrecondition` without a partial entry;
//! - an unpersisted Region dependency -> `RejectedMissingOrUnpersistedRegion`;
//! - immutable Region identity/content disagreement -> `RejectedIdentityConflict`;
//! - unavailable persistence -> `UnavailableBeforeAck`, never durable success.
//!
//! A fresh per-Agent journal has no current projection. Its first conditional
//! append establishes the authority tuple supplied by that request. A persisted
//! `FenceRevoked { generation }` advances the current Agent generation, so a
//! later request that still expects the prior generation is stale.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome, RegionPersisted,
};
use sha2::{Digest, Sha256};
use tempfile::TempDir;

const AGENT_ID: &str = "agent-c01";
const REGION_REF: &str = "region://agent-c01/action-spec";
const REGION_BYTES: &[u8] = b"action-spec-v1";
const BASE_PROJECTION: &str = "sha256:projection-7";

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn storage() -> (TempDir, D1DurableStorage) {
    let root = tempfile::tempdir().expect("temporary D1 root");
    let storage = D1DurableStorage::open(root.path()).expect("open D1 store");
    (root, storage)
}

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
            action_region_ref: REGION_REF.to_string(),
            action_digest: digest(REGION_BYTES),
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
fn region_persistence_refines_region_persisted_at_d1_across_reopen() {
    let (root, mut storage) = storage();
    let region_digest = digest(REGION_BYTES);

    assert_eq!(
        storage.ensure_region(
            REGION_REF,
            &region_digest,
            REGION_BYTES,
            DurabilityProfile::D1,
        ),
        EnsureRegionOutcome::Success(RegionPersisted {
            region_ref: REGION_REF.to_string(),
            content_digest: region_digest.clone(),
            profile: DurabilityProfile::D1,
        })
    );

    drop(storage);
    let reopened = D1DurableStorage::open(root.path()).expect("reopen D1 store");
    assert_eq!(
        reopened.read_region(REGION_REF),
        Some(REGION_BYTES.to_vec())
    );
}

#[test]
fn equal_region_replay_refines_idempotent_region_success() {
    let (_root, mut storage) = storage();
    let region_digest = digest(REGION_BYTES);
    let persisted = RegionPersisted {
        region_ref: REGION_REF.to_string(),
        content_digest: region_digest.clone(),
        profile: DurabilityProfile::D1,
    };

    assert_eq!(
        storage.ensure_region(
            REGION_REF,
            &region_digest,
            REGION_BYTES,
            DurabilityProfile::D1
        ),
        EnsureRegionOutcome::Success(persisted.clone())
    );
    assert_eq!(
        storage.ensure_region(
            REGION_REF,
            &region_digest,
            REGION_BYTES,
            DurabilityProfile::D1
        ),
        EnsureRegionOutcome::AlreadyPersistedSameContent(persisted)
    );
}

#[test]
fn conflicting_region_digest_refines_immutable_identity_rejection() {
    let (_root, mut storage) = storage();
    let region_digest = digest(REGION_BYTES);
    let different = b"different-content";

    assert!(matches!(
        storage.ensure_region(
            REGION_REF,
            &region_digest,
            REGION_BYTES,
            DurabilityProfile::D1
        ),
        EnsureRegionOutcome::Success(_)
    ));
    assert_eq!(
        storage.ensure_region(
            REGION_REF,
            &digest(different),
            different,
            DurabilityProfile::D1,
        ),
        EnsureRegionOutcome::RejectedIdentityConflict
    );
}

#[test]
fn conditional_append_refines_entry_persisted_for_current_authority() {
    let (_root, mut storage) = storage();
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
    let (_root, mut storage) = storage();
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
    let (_root, mut storage) = storage();
    let request = attempt_armed_request(303);
    let region_digest = digest(REGION_BYTES);

    assert!(matches!(
        storage.ensure_region(
            REGION_REF,
            &region_digest,
            REGION_BYTES,
            DurabilityProfile::D1
        ),
        EnsureRegionOutcome::Success(_)
    ));

    let proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("entry with a persisted Region must append, got {other:?}"),
    };

    assert_eq!(proof.agent_id, request.agent_id);
    assert_eq!(proof.entry_id, request.entry_id);
    assert_eq!(proof.entry_kind, "AttemptArmed");
    assert_eq!(proof.referenced_region_digests, vec![region_digest]);
}

#[test]
fn same_entry_retry_and_read_refine_lost_ack_recovery() {
    let (root, mut storage) = storage();
    let request = region_free_fence_request(301);

    let first_proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("first append must persist before its ack is lost, got {other:?}"),
    };
    drop(storage);

    let mut reopened = D1DurableStorage::open(root.path()).expect("reopen after lost ack");
    assert_eq!(
        reopened.append_conditional(request.clone()),
        AppendConditionalOutcome::AlreadyPersistedSameEntry(first_proof.clone())
    );
    assert_eq!(
        reopened.read_entry(&request.agent_id, request.entry_id),
        Some(first_proof.clone())
    );

    let mut conflicting_request = request.clone();
    conflicting_request.entry = CoreEntry::TurnCommitted {
        turn_id: 99,
        successor_projection_digest: None,
        action_manifest_digest: None,
        action_manifest: vec![],
    };

    assert!(matches!(
        reopened.append_conditional(conflicting_request),
        AppendConditionalOutcome::IntegrityFault
            | AppendConditionalOutcome::RejectedPrecondition { .. }
    ));
    assert_eq!(
        reopened.read_entry(&request.agent_id, request.entry_id),
        Some(first_proof)
    );
}

#[test]
fn unpersisted_region_refines_append_rejection_without_partial_entry() {
    let (_root, mut storage) = storage();
    let request = attempt_armed_request(302);

    assert_eq!(
        storage.append_conditional(request.clone()),
        AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion
    );
    assert_eq!(
        storage.read_entry(&request.agent_id, request.entry_id),
        None
    );

    let region_digest = digest(REGION_BYTES);
    assert!(matches!(
        storage.ensure_region(
            REGION_REF,
            &region_digest,
            REGION_BYTES,
            DurabilityProfile::D1
        ),
        EnsureRegionOutcome::Success(_)
    ));

    let proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!(
            "missing-Region rejection must not consume the entry identity; retry got {other:?}"
        ),
    };
    assert_eq!(proof.referenced_region_digests, vec![region_digest]);
}

#[test]
fn d1_core_store_enforces_one_live_writer_per_root() {
    let root = tempfile::tempdir().expect("D1 root");
    let first = D1DurableStorage::open(root.path()).expect("first writer owns root");

    assert!(
        D1DurableStorage::open(root.path()).is_err(),
        "a second live Core writer must not open the same root"
    );

    drop(first);
    D1DurableStorage::open(root.path()).expect("ownership is released when writer drops");
}

#[test]
fn uncertain_journal_write_poisoning_requires_reopen_before_more_transitions() {
    let root = tempfile::tempdir().expect("D1 root");
    let mut storage = D1DurableStorage::open(root.path()).expect("open D1 store");
    let request = AppendConditionalRequest {
        agent_id: "agent_uncertain".to_string(),
        entry_id: 1,
        expected_core_epoch: 1,
        expected_agent_generation: Some(1),
        expected_turn_id: Some(1),
        expected_lease_epoch: Some(1),
        expected_base_projection_digest: Some("projection-before-uncertain".to_string()),
        entry: CoreEntry::TurnCommitted {
            turn_id: 1,
            successor_projection_digest: None,
            action_manifest_digest: None,
            action_manifest: vec![],
        },
        region_refs: vec![],
    };
    storage.inject_failure_after_next_journal_write();

    assert_eq!(
        storage.append_conditional(request.clone()),
        AppendConditionalOutcome::UnavailableBeforeAck
    );
    assert!(!storage.is_healthy());
    assert_eq!(
        storage.append_conditional(request.clone()),
        AppendConditionalOutcome::IntegrityFault,
        "the uncertain live instance must not append or claim idempotence"
    );
    drop(storage);

    let mut recovered = D1DurableStorage::open(root.path()).expect("replay uncertain journal");
    assert!(recovered.is_healthy());
    assert!(matches!(
        recovered.append_conditional(request),
        AppendConditionalOutcome::AlreadyPersistedSameEntry(_)
    ));
}
