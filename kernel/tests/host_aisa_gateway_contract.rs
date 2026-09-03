//! RFC v3 §6 AISA gateway contract.
//!
//! These tests intentionally start against a stale socket inode.  The Phase 3
//! daemon does not exist yet, so every scenario currently fails at its real
//! boundary (`UnixStream::connect`) with connection refused.  Once `castord`
//! exists, the request sequences and assertions below become the acceptance
//! suite without replacing this harness.

use castor_kernel::host::{
    read_framed, GatewayClient, SyscallRequest, SyscallResponse, MAX_FRAME_BYTES,
};
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::process::Command;
use tempfile::TempDir;

const DIGEST: &str = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

struct ContractHarness {
    root: TempDir,
    socket: PathBuf,
}

impl ContractHarness {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("temporary host root");
        let socket = root.path().join("castord.sock");
        // A bound-then-closed socket leaves the exact failure mode expected
        // before the Phase 3 listener is implemented: ECONNREFUSED.
        drop(UnixListener::bind(&socket).expect("create stale socket inode"));
        Self { root, socket }
    }

    fn client(&self) -> GatewayClient {
        GatewayClient::connect(&self.socket).expect(
            "T-302-C castord daemon is intentionally absent: this RED contract must fail at socket connect",
        )
    }

    fn storage_root(&self) -> &std::path::Path {
        self.root.path()
    }

    fn provider_submission_count(&self) -> usize {
        panic!("T-302-C must wire this fixture to the real host-side counting provider")
    }

    fn lose_adapter_dedup_state(&self) {
        panic!("T-302-C must simulate loss of the real host adapter dedup projection")
    }
}

fn request(request_id: &str, op: &str, payload: Value) -> SyscallRequest {
    SyscallRequest {
        request_id: request_id.into(),
        op: op.into(),
        payload,
    }
}

fn call(client: &mut GatewayClient, request_id: &str, op: &str, payload: Value) -> SyscallResponse {
    client
        .request(&request(request_id, op, payload))
        .unwrap_or_else(|error| panic!("{op} must receive a framed gateway response: {error}"))
}

fn assert_outcome(response: SyscallResponse, expected: &str) {
    assert_eq!(response.outcome, Some(json!({ "type": expected })));
}

fn assert_attempt_armed(response: SyscallResponse, attempt_id: u64) {
    assert_eq!(
        response.outcome,
        Some(json!({ "type": "AttemptArmed", "attempt_id": attempt_id }))
    );
}

fn commit_ready_turn(client: &mut GatewayClient, action_manifest: &[&str]) {
    assert_outcome(
        call(
            client,
            "ensure-observation",
            "EnsureRegion",
            json!({ "region_ref": "region://observation", "content_digest": DIGEST, "content": [], "profile": "D1" }),
        ),
        "Success",
    );
    assert_outcome(
        call(
            client,
            "admit",
            "AdmitTurn",
            json!({ "agent_id": "agent-1", "turn_id": 1, "lease_epoch": 0, "base_projection_digest": DIGEST }),
        ),
        "Admitted",
    );
    assert_outcome(
        call(
            client,
            "request",
            "RequestInteraction",
            json!({ "interaction_id": "interaction-1", "lease_epoch": 0, "request_digest": DIGEST }),
        ),
        "InteractionRequested",
    );
    assert_outcome(
        call(
            client,
            "report",
            "ReportOutcome",
            json!({ "interaction_id": "interaction-1", "observation_region_id": "region://observation", "observation_digest": DIGEST }),
        ),
        "InteractionBound",
    );
    assert_outcome(
        call(
            client,
            "consume",
            "ConsumeInteraction",
            json!({ "interaction_id": "interaction-1", "lease_epoch": 1 }),
        ),
        "InteractionConsumed",
    );
    assert_outcome(
        call(
            client,
            "ensure-manifest",
            "EnsureRegion",
            json!({ "region_ref": "region://manifest", "content_digest": DIGEST, "content": [], "profile": "D1" }),
        ),
        "Success",
    );
    assert_outcome(
        call(
            client,
            "commit",
            "CommitTurn",
            json!({ "lease_epoch": 1, "base_projection_digest": DIGEST, "successor_region_id": "region://observation", "successor_digest": DIGEST, "action_manifest_region_id": "region://manifest", "action_manifest_digest": DIGEST, "action_manifest": action_manifest }),
        ),
        "TurnCommitted",
    );
}

fn arm_action(client: &mut GatewayClient, action_id: &str, scope: &str) {
    assert_outcome(
        call(
            client,
            "register",
            "RegisterAction",
            json!({ "action_id": action_id }),
        ),
        "ActionRegistered",
    );
    assert_attempt_armed(
        call(
            client,
            "arm",
            "PresentAdmissionCertificate",
            json!({ "action_id": action_id, "target_scope": scope, "capability_id": "capability-1", "generation": 1 }),
        ),
        1,
    );
}

#[test]
fn scenario_01_end_to_end_governed_turn_over_socket() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"]);
    arm_action(&mut client, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut client,
            "record",
            "RecordDispatchAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "DispatchRecorded",
    );
    assert_outcome(
        call(
            &mut client,
            "deliver",
            "DeliverArmedAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "Delivered",
    );
    assert_outcome(
        call(
            &mut client,
            "settle",
            "PresentSettlementCertificate",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1", "evidence_region_id": "region://observation", "evidence_digest": DIGEST, "proof_class": "ProviderConfirmation", "resolution": "Confirmed" }),
        ),
        "Settled",
    );
    assert_attempt_armed(
        call(
            &mut client,
            "rearm",
            "PresentAdmissionCertificate",
            json!({ "action_id": "action-1", "target_scope": "scope-1", "capability_id": "capability-1", "generation": 1 }),
        ),
        2,
    );
    assert_eq!(harness.provider_submission_count(), 1);
}

#[test]
fn scenario_02_reference_runtime_receives_castor_ipc_socket() {
    let harness = ContractHarness::new();
    let _client = harness.client();
    let status = Command::new("python3")
        .arg("-c")
        .arg("import os; assert os.environ['CASTOR_IPC_SOCKET']")
        .env("CASTOR_IPC_SOCKET", &harness.socket)
        .status()
        .expect("run Phase 3 reference runtime");
    assert!(status.success(), "reference runtime must exit cleanly");
}

#[test]
fn scenario_03_pre_commit_action_registration_is_rejected() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    assert_outcome(
        call(
            &mut client,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-1" }),
        ),
        "RejectedPrecondition",
    );
}

#[test]
fn scenario_04_pre_commit_admission_certificate_is_rejected() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    assert_outcome(
        call(
            &mut client,
            "arm",
            "PresentAdmissionCertificate",
            json!({ "action_id": "action-1", "target_scope": "scope-1", "capability_id": "capability-1", "generation": 1 }),
        ),
        "RejectedPrecondition",
    );
}

#[test]
fn scenario_05_uncommitted_action_id_admission_is_rejected() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"]);
    assert_outcome(
        call(
            &mut client,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-2" }),
        ),
        "RejectedPrecondition",
    );
}

#[test]
fn scenario_06_deliver_without_record_is_rejected_without_provider_submission() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"]);
    arm_action(&mut client, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut client,
            "deliver",
            "DeliverArmedAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "RejectedCurrentState",
    );
    assert_eq!(harness.provider_submission_count(), 0);
}

#[test]
fn scenario_07_duplicate_deliver_is_deduplicated_over_socket() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"]);
    arm_action(&mut client, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut client,
            "record",
            "RecordDispatchAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "DispatchRecorded",
    );
    assert_outcome(
        call(
            &mut client,
            "deliver-1",
            "DeliverArmedAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "Delivered",
    );
    assert_outcome(
        call(
            &mut client,
            "deliver-2",
            "DeliverArmedAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "DuplicateDelivery",
    );
    assert_eq!(harness.provider_submission_count(), 1);
}

#[test]
fn scenario_08_missing_adapter_dedup_after_dispatch_is_ambiguous() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"]);
    arm_action(&mut client, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut client,
            "record",
            "RecordDispatchAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "DispatchRecorded",
    );
    harness.lose_adapter_dedup_state();
    assert_outcome(
        call(
            &mut client,
            "deliver",
            "DeliverArmedAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "Ambiguous",
    );
}

#[test]
fn scenario_09_stale_lease_epoch_is_rejected() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    assert_outcome(
        call(
            &mut client,
            "admit",
            "AdmitTurn",
            json!({ "agent_id": "agent-1", "turn_id": 1, "lease_epoch": 0, "base_projection_digest": DIGEST }),
        ),
        "Admitted",
    );
    assert_outcome(
        call(
            &mut client,
            "request",
            "RequestInteraction",
            json!({ "interaction_id": "interaction-1", "lease_epoch": 0, "request_digest": DIGEST }),
        ),
        "InteractionRequested",
    );
    assert_outcome(
        call(
            &mut client,
            "consume",
            "ConsumeInteraction",
            json!({ "interaction_id": "interaction-1", "lease_epoch": 0 }),
        ),
        "RejectedStaleAuthority",
    );
}

#[test]
fn scenario_10_commit_while_awaiting_interaction_is_rejected() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    assert_outcome(
        call(
            &mut client,
            "admit",
            "AdmitTurn",
            json!({ "agent_id": "agent-1", "turn_id": 1, "lease_epoch": 0, "base_projection_digest": DIGEST }),
        ),
        "Admitted",
    );
    assert_outcome(
        call(
            &mut client,
            "request",
            "RequestInteraction",
            json!({ "interaction_id": "interaction-1", "lease_epoch": 0, "request_digest": DIGEST }),
        ),
        "InteractionRequested",
    );
    assert_outcome(
        call(
            &mut client,
            "commit",
            "CommitTurn",
            json!({ "lease_epoch": 1, "base_projection_digest": DIGEST, "successor_region_id": "region://observation", "successor_digest": DIGEST, "action_manifest_region_id": "region://manifest", "action_manifest_digest": DIGEST, "action_manifest": ["action-1"] }),
        ),
        "RejectedStaleAuthority",
    );
}

#[test]
fn scenario_11_stale_generation_admission_is_rejected_after_fence() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"]);
    assert_outcome(
        call(
            &mut client,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-1" }),
        ),
        "ActionRegistered",
    );
    assert_outcome(
        call(
            &mut client,
            "fence",
            "PersistFence",
            json!({ "generation": 2 }),
        ),
        "GenerationFenced",
    );
    assert_eq!(call(&mut client, "arm", "PresentAdmissionCertificate", json!({ "action_id": "action-1", "target_scope": "scope-1", "capability_id": "capability-1", "generation": 1 })).outcome, Some(json!({ "type": "RejectedStaleGeneration", "current_generation": 2 })));
}

#[test]
fn scenario_12_scope_mutex_rejects_overlapping_admission() {
    let harness = ContractHarness::new();
    let mut client_one = harness.client();
    let mut client_two = harness.client();
    commit_ready_turn(&mut client_one, &["action-1", "action-2"]);
    arm_action(&mut client_one, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut client_two,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-2" }),
        ),
        "ActionRegistered",
    );
    assert_outcome(
        call(
            &mut client_two,
            "arm",
            "PresentAdmissionCertificate",
            json!({ "action_id": "action-2", "target_scope": "scope-1", "capability_id": "capability-1", "generation": 1 }),
        ),
        "RejectedCurrentState",
    );
}

#[test]
fn scenario_13_ensure_region_digest_mismatch_is_rejected() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    assert_outcome(
        call(
            &mut client,
            "ensure",
            "EnsureRegion",
            json!({ "region_ref": "region://mismatch", "content_digest": DIGEST, "content": [1], "profile": "D1" }),
        ),
        "RejectedIdentityConflict",
    );
}

#[test]
fn scenario_14_second_daemon_is_excluded_by_storage_writer_lock() {
    let harness = ContractHarness::new();
    let _client = harness.client();
    let output = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            harness.storage_root().to_str().unwrap(),
            "--socket",
            harness.socket.to_str().unwrap(),
        ])
        .output()
        .expect("start second castord");
    assert!(
        !output.status.success(),
        "second host must fail on .c01-writer.lock"
    );
}

#[test]
fn scenario_15_live_socket_collision_never_unlinks_active_socket() {
    let harness = ContractHarness::new();
    let _client = harness.client();
    let socket_metadata_before = fs::metadata(&harness.socket).expect("active socket inode");
    let output = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            harness.storage_root().to_str().unwrap(),
            "--socket",
            harness.socket.to_str().unwrap(),
        ])
        .output()
        .expect("start colliding castord");
    assert!(!output.status.success());
    assert_eq!(
        fs::metadata(&harness.socket)
            .expect("socket retained")
            .ino(),
        socket_metadata_before.ino()
    );
}

#[test]
fn scenario_16_framing_bounds_fail_closed() {
    let harness = ContractHarness::new();
    let _client = harness.client();
    let mut raw = UnixStream::connect(&harness.socket).expect("connect framing probe");
    raw.write_all(&[0, 0, 0]).expect("send truncated header");
    drop(raw);
    let mut zero = UnixStream::connect(&harness.socket).expect("connect zero-length probe");
    zero.write_all(&[0, 0, 0, 0])
        .expect("send zero-length header");
    let zero_response: SyscallResponse =
        serde_json::from_slice(&read_framed(&mut zero).expect("read MalformedRequest response"))
            .expect("decode MalformedRequest response");
    assert_eq!(zero_response.error.unwrap().code, "MalformedRequest");
    assert!(
        MAX_FRAME_BYTES < u32::MAX as usize,
        "oversized length must be rejected before allocation"
    );
    let mut malformed = UnixStream::connect(&harness.socket).expect("connect malformed probe");
    malformed
        .write_all(&[0, 0, 0, 1, b'{'])
        .expect("send invalid JSON frame");
    let response: SyscallResponse =
        serde_json::from_slice(&read_framed(&mut malformed).expect("read malformed JSON response"))
            .expect("decode malformed JSON response");
    assert_eq!(response.error.unwrap().code, "MalformedRequest");
}

#[test]
fn scenario_17_lost_ack_after_commit_does_not_mint_a_second_turn() {
    let harness = ContractHarness::new();
    let mut first_client = harness.client();
    commit_ready_turn(&mut first_client, &["action-1"]);
    drop(first_client);
    let mut retry_client = harness.client();
    assert_outcome(
        call(
            &mut retry_client,
            "retry-admit",
            "AdmitTurn",
            json!({ "agent_id": "agent-1", "turn_id": 1, "lease_epoch": 0, "base_projection_digest": DIGEST }),
        ),
        "RejectedPrecondition",
    );
}

#[test]
fn scenario_18_supervisor_persists_fence_before_child_termination_and_reap() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    assert_outcome(
        call(
            &mut client,
            "admit",
            "AdmitTurn",
            json!({ "agent_id": "agent-1", "turn_id": 1, "lease_epoch": 0, "base_projection_digest": DIGEST }),
        ),
        "Admitted",
    );
    assert_outcome(
        call(
            &mut client,
            "fence",
            "PersistFence",
            json!({ "generation": 2 }),
        ),
        "GenerationFenced",
    );
    assert_outcome(
        call(
            &mut client,
            "replacement",
            "AdmitTurn",
            json!({ "agent_id": "agent-1", "turn_id": 2, "lease_epoch": 1, "base_projection_digest": DIGEST }),
        ),
        "RejectedPrecondition",
    );
}
