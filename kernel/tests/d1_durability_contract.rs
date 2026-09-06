//! EPIC-29 D1 durability and recovery hostile contracts.
//!
//! These tests deliberately describe the framed-journal and D-04 recovery
//! boundary accepted in RFC v2.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use castor_kernel::c06_composition::{
    AdmitTurnRequest, CapabilityGrant, CapabilityRight, CommitTurnRequest,
    ConsumeInteractionRequest, D1GovernedTurnAuthority, DeliverArmedAttemptRequest,
    GovernedTurnOutcome, GrantCapabilityRequest, InteractionOutcomeReport,
    PresentAdmissionCertificateRequest, RecordDispatchAttemptRequest, RequestInteractionRequest,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use tempfile::TempDir;

const AGENT: &str = "agent-d1-contract";
const BASE: &str = "sha256:projection-d1-contract";

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn open() -> (TempDir, D1DurableStorage) {
    let root = tempfile::tempdir().expect("temporary D1 root");
    let storage = D1DurableStorage::open(root.path()).expect("open D1 storage");
    (root, storage)
}

fn authority(root: &TempDir) -> D1GovernedTurnAuthority {
    D1GovernedTurnAuthority::for_test(
        D1DurableStorage::open(root.path()).expect("open authority storage"),
    )
}

fn grant(cap_id: &str, rights: Vec<CapabilityRight>) -> GrantCapabilityRequest {
    GrantCapabilityRequest {
        grant: CapabilityGrant {
            cap_id: cap_id.into(),
            subject: AGENT.into(),
            object_ref: "c04:http_get".into(),
            rights,
            constraints: vec![],
            parent_cap_id: None,
            revocation_domain: None,
            delegation_allowed: true,
            max_turns: None,
        },
    }
}

fn admit(cap_id: Option<&str>) -> AdmitTurnRequest {
    AdmitTurnRequest {
        agent_id: AGENT.into(),
        turn_id: 1,
        lease_epoch: 0,
        base_projection_digest: BASE.into(),
        cap_id: cap_id.map(str::to_owned),
    }
}

fn persist_region(authority: &mut D1GovernedTurnAuthority, region: &str, bytes: &[u8]) {
    assert!(matches!(
        authority.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
        EnsureRegionOutcome::Success(_)
    ));
}

fn commit_request(lease_epoch: u64) -> CommitTurnRequest {
    CommitTurnRequest {
        lease_epoch,
        base_projection_digest: BASE.into(),
        successor_region_id: "region://successor".into(),
        successor_digest: digest(b"successor"),
        action_manifest_region_id: "region://actions".into(),
        action_manifest_digest: digest(b"action-1"),
        action_manifest: vec!["action-1".into()],
        cap_id: None,
    }
}

fn ready_to_commit(authority: &mut D1GovernedTurnAuthority) {
    persist_region(authority, "region://observation", b"observation");
    persist_region(authority, "region://successor", b"successor");
    persist_region(authority, "region://actions", b"action-1");
    assert_eq!(
        authority.admit_turn(admit(None)),
        GovernedTurnOutcome::Admitted
    );
    assert_eq!(
        authority.request_interaction(RequestInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 0,
            request_digest: digest(b"request"),
        }),
        GovernedTurnOutcome::InteractionRequested
    );
    assert_eq!(
        authority.report_outcome(InteractionOutcomeReport {
            interaction_id: "interaction-1".into(),
            observation_region_id: "region://observation".into(),
            observation_digest: digest(b"observation"),
        }),
        GovernedTurnOutcome::InteractionBound
    );
}

fn armed_dispatched_attempt(authority: &mut D1GovernedTurnAuthority) {
    assert_eq!(
        authority.grant_capability(grant(
            "cap-1",
            vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
        )),
        GovernedTurnOutcome::CapabilityGranted
    );
    persist_region(authority, "region://successor", b"successor");
    persist_region(authority, "region://actions", b"action-1");
    assert_eq!(
        authority.admit_turn(admit(Some("cap-1"))),
        GovernedTurnOutcome::Admitted
    );
    assert_eq!(
        authority.commit_turn(CommitTurnRequest {
            cap_id: Some("cap-1".into()),
            ..commit_request(0)
        }),
        GovernedTurnOutcome::TurnCommitted
    );
    assert_eq!(
        authority.register_action(castor_kernel::c06_composition::ActionRegistrationRequest {
            action_id: "action-1".into(),
            agent_id: AGENT.into(),
            action_family: "c04:http_get".into(),
            cap_id: "cap-1".into(),
            target_scope: "scope".into(),
            numeric_parameters: BTreeMap::new(),
            exact_parameters: BTreeMap::new(),
        }),
        GovernedTurnOutcome::ActionRegistered
    );
    assert_eq!(
        authority.present_admission_certificate(PresentAdmissionCertificateRequest {
            action_id: "action-1".into(),
            target_scope: "scope".into(),
            capability_id: "cap-1".into(),
            generation: 1
        }),
        GovernedTurnOutcome::AttemptArmed { attempt_id: 1 }
    );
    assert_eq!(
        authority.record_dispatch_attempt(RecordDispatchAttemptRequest {
            attempt_id: 1,
            dispatch_identity: "dispatch-1".into()
        }),
        GovernedTurnOutcome::DispatchRecorded
    );
}

fn request(entry_id: u64, entry: CoreEntry) -> AppendConditionalRequest {
    AppendConditionalRequest {
        agent_id: AGENT.into(),
        entry_id,
        expected_core_epoch: 1,
        expected_agent_generation: Some(1),
        expected_turn_id: None,
        expected_lease_epoch: None,
        expected_base_projection_digest: Some(BASE.into()),
        entry,
        region_refs: vec![],
    }
}

fn fence(entry_id: u64, generation: u64) -> AppendConditionalRequest {
    let mut request = request(entry_id, CoreEntry::FenceRevoked { generation });
    request.expected_agent_generation = Some(generation - 1);
    request
}

fn persist(storage: &mut D1DurableStorage, request: AppendConditionalRequest) {
    assert!(matches!(
        storage.append_conditional(request),
        AppendConditionalOutcome::EntryPersisted(_)
    ));
}

fn append_incomplete_frame(root: &Path) {
    let mut journal = OpenOptions::new()
        .create(true)
        .append(true)
        .open(root.join("core-journal.log"))
        .expect("open framed Core journal");
    journal
        .write_all(&16_u32.to_le_bytes())
        .expect("write declared payload length");
    journal
        .write_all(b"incomplete")
        .expect("write torn payload");
    journal.sync_all().expect("persist torn frame");
}

fn append_complete_bad_frame(root: &Path) {
    let payload = b"complete-but-corrupted";
    let mut journal = OpenOptions::new()
        .create(true)
        .append(true)
        .open(root.join("core-journal.log"))
        .expect("open framed Core journal");
    journal
        .write_all(&(payload.len() as u32).to_le_bytes())
        .expect("write length");
    journal.write_all(payload).expect("write payload");
    journal
        .write_all(&0_u32.to_le_bytes())
        .expect("write intentionally bad CRC");
    journal.sync_all().expect("persist corrupt frame");
}

fn region_backed_attempt(
    storage: &mut D1DurableStorage,
    entry_id: u64,
) -> AppendConditionalRequest {
    let bytes = b"durable action bytes";
    let region = "region://d1-contract/action";
    assert!(matches!(
        storage.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
        EnsureRegionOutcome::Success(_)
    ));
    let mut record = request(
        entry_id,
        CoreEntry::AttemptArmed {
            action_id: "action-1".into(),
            attempt_id: 1,
            action_region_ref: region.into(),
            action_digest: digest(bytes),
            request_digest: "sha256:request".into(),
        },
    );
    record.region_refs = vec![region.into()];
    record
}

#[test]
fn test_d1_incomplete_tail_frame_auto_truncated() {
    let (root, mut storage) = open();
    persist(&mut storage, fence(1, 2));
    let valid_prefix_len = fs::metadata(root.path().join("core-journal.log"))
        .expect("Phase 3 framed journal exists")
        .len();
    drop(storage);
    append_incomplete_frame(root.path());

    let mut reopened = D1DurableStorage::open(root.path()).expect("truncate incomplete tail");
    assert_eq!(
        fs::metadata(root.path().join("core-journal.log"))
            .unwrap()
            .len(),
        valid_prefix_len
    );
    persist(&mut reopened, fence(2, 3));
}

#[test]
fn test_d2_mid_file_corruption_fails_closed() {
    let (root, mut storage) = open();
    for generation in 2..=5 {
        persist(&mut storage, fence(generation - 1, generation));
    }
    drop(storage);
    let journal = root.path().join("core-journal.log");
    let mut bytes = fs::read(&journal).unwrap();
    let first_payload_len =
        u32::from_le_bytes(bytes[..4].try_into().expect("first frame length")) as usize;
    let second_frame = 4 + first_payload_len + 4;
    bytes[second_frame + 4] ^= 0x01;
    fs::write(&journal, bytes).unwrap();
    assert!(D1DurableStorage::open(root.path()).is_err());
}

#[test]
fn test_d3_missing_region_fails_closed() {
    let (root, mut storage) = open();
    let armed = region_backed_attempt(&mut storage, 1);
    persist(&mut storage, armed);
    let region_file = fs::read_dir(root.path().join("regions"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().is_some_and(|extension| extension == "bin"))
        .unwrap();
    drop(storage);
    fs::remove_file(region_file).unwrap();
    assert!(D1DurableStorage::open(root.path()).is_err());
}

#[test]
fn test_d4_crash_advances_core_epoch_staling_pre_crash_lease() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    ready_to_commit(&mut authority);
    assert_eq!(
        authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 1
        }),
        GovernedTurnOutcome::InteractionConsumed
    );
    assert_eq!(authority.core_epoch(), 1);
    assert_eq!(
        authority.reconstruct_after_crash(),
        GovernedTurnOutcome::Reconstructed
    );
    assert_eq!(authority.core_epoch(), 2);
    assert_eq!(
        authority.commit_turn(commit_request(1)),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
}

#[test]
fn test_d5_committed_turn_permanence_inherited_sentinel() {
    let (root, mut storage) = open();
    persist(
        &mut storage,
        request(
            1,
            CoreEntry::TurnCommitted {
                turn_id: 1,
                successor_projection_digest: Some("sha256:successor".into()),
                action_manifest_digest: Some("sha256:manifest".into()),
                action_manifest: vec!["action-1".into()],
                cap_id: None,
            },
        ),
    );
    drop(storage);
    let mut recovered = D1DurableStorage::open(root.path()).expect("recover committed turn");
    let mut rewrite = request(
        1,
        CoreEntry::TurnCommitted {
            turn_id: 1,
            successor_projection_digest: Some("sha256:other".into()),
            action_manifest_digest: None,
            action_manifest: vec![],
            cap_id: None,
        },
    );
    rewrite.expected_core_epoch = 2;
    assert!(matches!(
        recovered.append_conditional(rewrite),
        AppendConditionalOutcome::IntegrityFault
    ));
}

#[test]
fn test_d6_armed_unknown_and_scope_mutex_permanence() {
    let (root, mut storage) = open();
    let armed = region_backed_attempt(&mut storage, 1);
    persist(&mut storage, armed);
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("recover armed attempt");
    assert_eq!(
        recovered.journal_entries(),
        1,
        "recovery must retain ArmedUnknown"
    );
    assert!(
        recovered.read_entry(AGENT, 1).is_some(),
        "armed fact remains durable"
    );
}

#[test]
fn test_d7_provider_dedup_resets_ram_counter_and_duplicate_delivery() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    armed_dispatched_attempt(&mut authority);
    assert_eq!(
        authority.deliver_armed_attempt(DeliverArmedAttemptRequest {
            attempt_id: 1,
            dispatch_identity: "dispatch-1".into()
        }),
        GovernedTurnOutcome::Delivered
    );
    assert_eq!(authority.provider_submission_count(), 1);
    assert_eq!(
        authority.reconstruct_after_crash(),
        GovernedTurnOutcome::Reconstructed
    );
    assert_eq!(authority.provider_submission_count(), 0);
    assert_eq!(
        authority.deliver_armed_attempt(DeliverArmedAttemptRequest {
            attempt_id: 1,
            dispatch_identity: "dispatch-1".into()
        }),
        GovernedTurnOutcome::DuplicateDelivery
    );
    assert_eq!(authority.provider_submission_count(), 0);
}

#[test]
fn test_d8_revocation_permanence_inherited_sentinel() {
    let (root, mut storage) = open();
    persist(
        &mut storage,
        request(
            1,
            CoreEntry::CapabilityRevoked {
                capability_id: "cap-1".into(),
            },
        ),
    );
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("recover revocation");
    assert_eq!(recovered.journal_requests().len(), 1);
    assert!(matches!(
        recovered.journal_requests()[0].entry,
        CoreEntry::CapabilityRevoked { .. }
    ));
}

#[test]
fn test_d9_fence_generation_permanence_inherited_sentinel() {
    let (root, mut storage) = open();
    persist(&mut storage, fence(1, 2));
    drop(storage);
    let mut recovered = D1DurableStorage::open(root.path()).expect("recover fence");
    assert!(matches!(
        recovered.append_conditional(fence(2, 1)),
        AppendConditionalOutcome::RejectedPrecondition { .. }
    ));
}

fn write_snapshot(root: &Path, name: &str, bytes: &[u8]) {
    let snapshots = root.join("snapshots");
    fs::create_dir_all(&snapshots).unwrap();
    fs::write(snapshots.join(name), bytes).unwrap();
}

#[test]
fn test_d10_snapshot_plus_tail_replay_reconstructs_state() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    assert_eq!(
        authority.grant_capability(grant("cap-1", vec![CapabilityRight::AdmitTurn])),
        GovernedTurnOutcome::CapabilityGranted
    );
    assert_eq!(
        authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    authority.create_snapshot("snapshot-k").unwrap();
    assert_eq!(
        authority.persist_fence(3),
        GovernedTurnOutcome::GenerationFenced { generation: 3 }
    );
    drop(authority);
    assert_eq!(
        D1GovernedTurnAuthority::open(root.path())
            .unwrap()
            .generation(),
        3
    );
}

#[test]
fn test_d11_corrupted_snapshot_falls_back_to_genesis_replay() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    assert_eq!(
        authority.grant_capability(grant("cap-1", vec![CapabilityRight::AdmitTurn])),
        GovernedTurnOutcome::CapabilityGranted
    );
    assert_eq!(
        authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    authority.create_snapshot("snapshot-corrupt").unwrap();
    assert_eq!(
        authority.persist_fence(3),
        GovernedTurnOutcome::GenerationFenced { generation: 3 }
    );
    let snapshot = root.path().join("snapshots/snapshot-corrupt.json");
    let mut bytes = fs::read(&snapshot).unwrap();
    let snapshot_id_offset = bytes
        .windows(b"snapshot-corrupt".len())
        .position(|window| window == b"snapshot-corrupt")
        .unwrap();
    bytes[snapshot_id_offset] = b'S';
    fs::write(snapshot, bytes).unwrap();
    drop(authority);
    assert_eq!(
        D1GovernedTurnAuthority::open(root.path())
            .unwrap()
            .generation(),
        3
    );
}

#[test]
fn test_d12_leftover_snapshot_tempfile_ignored() {
    let (root, storage) = open();
    write_snapshot(root.path(), ".snapshot-crashed.tmp", b"incomplete snapshot");
    assert!(storage.latest_snapshot().is_none());
    drop(storage);
    D1GovernedTurnAuthority::open(root.path()).expect("ignore snapshot tempfile");
}

#[test]
fn test_d13_complete_tail_frame_bad_crc_fails_closed() {
    let (root, storage) = open();
    drop(storage);
    append_complete_bad_frame(root.path());
    assert!(D1DurableStorage::open(root.path()).is_err());
}

#[test]
fn test_d14_lost_ack_complete_frame_recovered_h04() {
    let (root, mut storage) = open();
    let entry = fence(1, 2);
    storage.inject_failure_after_next_journal_write();
    assert_eq!(
        storage.append_conditional(entry.clone()),
        AppendConditionalOutcome::UnavailableBeforeAck
    );
    drop(storage);
    let mut recovered =
        D1DurableStorage::open(root.path()).expect("recover complete unacknowledged frame");
    assert!(matches!(
        recovered.append_conditional(entry),
        AppendConditionalOutcome::AlreadyPersistedSameEntry(_)
    ));
    assert!(recovered.root().join("core-journal.log").exists());
}

#[test]
fn test_d15_fresh_lease_acquired_after_restart_for_open_turn() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    ready_to_commit(&mut authority);
    drop(authority);
    let mut recovered = D1GovernedTurnAuthority::open(root.path()).unwrap();
    assert_eq!(
        recovered.consume_interaction(ConsumeInteractionRequest {
            interaction_id: "interaction-1".into(),
            lease_epoch: 2
        }),
        GovernedTurnOutcome::InteractionConsumed
    );
    assert_eq!(
        recovered.commit_turn(commit_request(2)),
        GovernedTurnOutcome::TurnCommitted
    );
}

#[test]
fn test_d16_snapshot_then_fence_preserves_new_generation() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    assert_eq!(
        authority.grant_capability(grant("cap-1", vec![CapabilityRight::AdmitTurn])),
        GovernedTurnOutcome::CapabilityGranted
    );
    assert_eq!(
        authority.persist_fence(7),
        GovernedTurnOutcome::GenerationFenced { generation: 7 }
    );
    authority.create_snapshot("at-seven").unwrap();
    assert_eq!(
        authority.persist_fence(8),
        GovernedTurnOutcome::GenerationFenced { generation: 8 }
    );
    drop(authority);
    assert_eq!(
        D1GovernedTurnAuthority::open(root.path())
            .unwrap()
            .generation(),
        8
    );
}

#[test]
fn test_d17_snapshot_then_cap_revocation_blocks_exercise() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    assert_eq!(
        authority.grant_capability(grant("cap-1", vec![CapabilityRight::AdmitTurn])),
        GovernedTurnOutcome::CapabilityGranted
    );
    authority.create_snapshot("cap-valid").unwrap();
    assert_eq!(
        authority.revoke_capability("cap-1"),
        GovernedTurnOutcome::CapabilityRevoked
    );
    drop(authority);
    let mut recovered = D1GovernedTurnAuthority::open(root.path()).unwrap();
    assert!(recovered.is_capability_revoked("cap-1"));
    assert_eq!(
        recovered.admit_turn(admit(Some("cap-1"))),
        GovernedTurnOutcome::RejectedCapabilityRevoked
    );
}

#[test]
fn test_d18_action_registered_capability_binding_survives_restart() {
    let (root, mut storage) = open();
    persist(
        &mut storage,
        request(
            1,
            CoreEntry::ActionRegistered {
                target_scope: None,
                action_id: "action-1".into(),
                cap_id: "cap-1".into(),
            },
        ),
    );
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("recover action binding");
    assert!(matches!(
        recovered.journal_requests()[0].entry,
        CoreEntry::ActionRegistered { ref cap_id, .. } if cap_id == "cap-1"
    ));
}

#[test]
fn test_d19_unpointed_snapshot_blob_ignored() {
    let root = tempfile::tempdir().unwrap();
    let mut authority = authority(&root);
    assert_eq!(
        authority.grant_capability(grant("cap-1", vec![CapabilityRight::AdmitTurn])),
        GovernedTurnOutcome::CapabilityGranted
    );
    write_snapshot(root.path(), "unpointed.json", b"must not become authority");
    drop(authority);
    let storage = D1DurableStorage::open(root.path()).unwrap();
    assert!(storage.latest_snapshot().is_none());
    drop(storage);
    assert_eq!(
        D1GovernedTurnAuthority::open(root.path())
            .unwrap()
            .generation(),
        1
    );
}

#[test]
fn test_d20_snapshot_plus_tail_projection_equivalence_to_genesis() {
    let fast_root = tempfile::tempdir().unwrap();
    let genesis_root = tempfile::tempdir().unwrap();
    let mut fast = authority(&fast_root);
    let mut genesis = authority(&genesis_root);
    for authority in [&mut fast, &mut genesis] {
        assert_eq!(
            authority.grant_capability(grant(
                "cap-1",
                vec![CapabilityRight::AdmitTurn, CapabilityRight::Derive]
            )),
            GovernedTurnOutcome::CapabilityGranted
        );
        assert_eq!(
            authority.admit_turn(admit(None)),
            GovernedTurnOutcome::Admitted
        );
        assert_eq!(
            authority.persist_fence(2),
            GovernedTurnOutcome::GenerationFenced { generation: 2 }
        );
        let mut second_turn = admit(None);
        second_turn.turn_id = 2;
        assert_eq!(
            authority.admit_turn(second_turn),
            GovernedTurnOutcome::Admitted
        );
    }
    fast.create_snapshot("equivalent").unwrap();
    for authority in [&mut fast, &mut genesis] {
        assert!(matches!(
            authority.derive_capability(castor_kernel::c06_composition::DeriveCapabilityRequest {
                parent_cap_id: "cap-1".into(),
                child_subject: AGENT.into(),
                child_rights: vec![CapabilityRight::AdmitTurn],
                child_object_ref: "c04:http_get".into(),
                child_constraints: vec![],
                child_delegation_allowed: false
            }),
            castor_kernel::c06_composition::CapabilityDeriveOutcome::Derived { .. }
        ));
        assert_eq!(
            authority.persist_fence(3),
            GovernedTurnOutcome::GenerationFenced { generation: 3 }
        );
    }
    drop(fast);
    drop(genesis);
    let fast_path = D1GovernedTurnAuthority::open(fast_root.path()).unwrap();
    let genesis_path = D1GovernedTurnAuthority::open(genesis_root.path()).unwrap();
    assert!(fast_path.projection_equals(&genesis_path));
}
