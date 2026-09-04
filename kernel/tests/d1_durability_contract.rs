//! EPIC-29 D1 durability and recovery hostile contracts.
//!
//! These tests deliberately describe the framed-journal and D-04 recovery
//! boundary accepted in RFC v2.  They are RED until T-305-C supplies that
//! implementation; no test here changes the current JSONL contract.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use sha2::{Digest, Sha256};
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
    fs::write(&journal, [8_u8, 0, 0, 0, b'{', b'x', b'}', 0, 0, 0, 0]).unwrap();
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
    let (root, mut storage) = open();
    persist(
        &mut storage,
        request(
            1,
            CoreEntry::LeaseGranted {
                turn_id: 1,
                lease_epoch: 1,
            },
        ),
    );
    drop(storage);
    let mut recovered = D1DurableStorage::open(root.path()).expect("recover journal");
    let mut stale = request(
        2,
        CoreEntry::TurnCommitted {
            turn_id: 1,
            successor_projection_digest: None,
            action_manifest_digest: None,
            action_manifest: vec![],
            cap_id: None,
        },
    );
    stale.expected_turn_id = Some(1);
    stale.expected_lease_epoch = Some(1);
    stale.expected_core_epoch = 2;
    assert!(matches!(
        recovered.append_conditional(stale),
        AppendConditionalOutcome::RejectedPrecondition { .. }
    ));
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
    let (root, mut storage) = open();
    persist(
        &mut storage,
        request(1, CoreEntry::AdapterSubmissionRecorded { attempt_id: 1 }),
    );
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("recover durable submission");
    assert_eq!(
        recovered.journal_entries(),
        1,
        "durable dedup fact survives recovery"
    );
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

fn snapshot_index(
    entry_id: u64,
    snapshot_id: &str,
    last_entry_id: u64,
) -> AppendConditionalRequest {
    let mut request = request(
        entry_id,
        CoreEntry::SnapshotIndex {
            snapshot_id: snapshot_id.into(),
            last_entry_id,
            snapshot_digest: "sha256:snapshot".into(),
        },
    );
    request.expected_agent_generation = Some(2);
    request
}

fn write_snapshot(root: &Path, name: &str, bytes: &[u8]) {
    let snapshots = root.join("snapshots");
    fs::create_dir_all(&snapshots).unwrap();
    fs::write(snapshots.join(name), bytes).unwrap();
}

#[test]
fn test_d10_snapshot_plus_tail_replay_reconstructs_state() {
    let (root, mut storage) = open();
    persist(&mut storage, fence(1, 2));
    persist(&mut storage, snapshot_index(2, "snapshot-k", 1));
    persist(&mut storage, fence(3, 3));
    write_snapshot(root.path(), "snapshot-k.json", b"snapshot at entry one");
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("recover snapshot plus tail");
    assert!(matches!(
        recovered.journal_requests().last().unwrap().entry,
        CoreEntry::FenceRevoked { generation: 3 }
    ));
}

#[test]
fn test_d11_corrupted_snapshot_falls_back_to_genesis_replay() {
    let (root, mut storage) = open();
    persist(&mut storage, fence(1, 2));
    persist(&mut storage, snapshot_index(2, "bad", 1));
    write_snapshot(root.path(), "snapshot-bad.json", b"bad checksum");
    drop(storage);
    let recovered =
        D1DurableStorage::open(root.path()).expect("bad snapshot falls back to genesis");
    assert_eq!(recovered.journal_entries(), 1);
}

#[test]
fn test_d12_leftover_snapshot_tempfile_ignored() {
    let (root, storage) = open();
    write_snapshot(root.path(), ".snapshot-crashed.tmp", b"incomplete snapshot");
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("ignore snapshot tempfile");
    assert!(recovered.is_empty());
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
    let (root, mut storage) = open();
    persist(
        &mut storage,
        request(
            1,
            CoreEntry::LeaseGranted {
                turn_id: 1,
                lease_epoch: 1,
            },
        ),
    );
    drop(storage);
    let mut recovered = D1DurableStorage::open(root.path()).expect("recover open turn");
    let mut fresh = request(
        2,
        CoreEntry::LeaseGranted {
            turn_id: 1,
            lease_epoch: 2,
        },
    );
    fresh.expected_turn_id = Some(1);
    fresh.expected_lease_epoch = Some(1);
    fresh.expected_core_epoch = 2;
    assert!(matches!(
        recovered.append_conditional(fresh),
        AppendConditionalOutcome::EntryPersisted(_)
    ));
}

#[test]
fn test_d16_snapshot_then_fence_preserves_new_generation() {
    let (root, mut storage) = open();
    let mut initial_fence = fence(1, 7);
    initial_fence.expected_agent_generation = Some(1);
    persist(&mut storage, initial_fence);
    let mut index = snapshot_index(2, "at-seven", 1);
    index.expected_agent_generation = Some(7);
    persist(&mut storage, index);
    persist(&mut storage, fence(3, 8));
    write_snapshot(root.path(), "snapshot-at-seven.json", b"generation seven");
    drop(storage);
    let mut recovered = D1DurableStorage::open(root.path()).expect("recover snapshot tail fence");
    assert!(matches!(
        recovered.append_conditional(fence(4, 7)),
        AppendConditionalOutcome::RejectedPrecondition { .. }
    ));
}

#[test]
fn test_d17_snapshot_then_cap_revocation_blocks_exercise() {
    let (root, mut storage) = open();
    persist(&mut storage, snapshot_index(1, "cap-valid", 0));
    persist(
        &mut storage,
        request(
            2,
            CoreEntry::CapabilityRevoked {
                capability_id: "cap-1".into(),
            },
        ),
    );
    write_snapshot(root.path(), "snapshot-cap-valid.json", b"cap-1 valid at K");
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("recover revocation tail");
    assert_eq!(
        recovered.journal_entries(),
        1,
        "D-04 must replay revocation tail only once"
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
    let (root, mut storage) = open();
    persist(&mut storage, fence(1, 2));
    write_snapshot(
        root.path(),
        "snapshot-unpointed.json",
        b"must not become authority",
    );
    drop(storage);
    let recovered = D1DurableStorage::open(root.path()).expect("ignore unpointed snapshot");
    assert_eq!(recovered.journal_entries(), 1);
}

#[test]
fn test_d20_snapshot_plus_tail_projection_equivalence_to_genesis() {
    let (root, mut storage) = open();
    persist(&mut storage, fence(1, 2));
    persist(&mut storage, snapshot_index(2, "equivalent", 1));
    persist(&mut storage, fence(3, 3));
    write_snapshot(
        root.path(),
        "snapshot-equivalent.json",
        b"projection at generation two",
    );
    drop(storage);
    let fast_path = D1DurableStorage::open(root.path()).expect("snapshot recovery");
    fs::remove_file(root.path().join("snapshots/snapshot-equivalent.json")).unwrap();
    let genesis_path = tempfile::tempdir().unwrap();
    fs::create_dir_all(genesis_path.path().join("regions")).unwrap();
    fs::copy(
        root.path().join("core-journal.log"),
        genesis_path.path().join("core-journal.log"),
    )
    .unwrap();
    let genesis = D1DurableStorage::open(genesis_path.path()).expect("genesis replay");
    assert_eq!(fast_path.journal_requests(), genesis.journal_requests());
}
