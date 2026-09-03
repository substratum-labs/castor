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
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{ErrorKind, Write};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
use tempfile::TempDir;

const DIGEST: &str = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

struct ContractHarness {
    _serial: MutexGuard<'static, ()>,
    root: TempDir,
    socket: PathBuf,
    daemon: Mutex<Child>,
}

impl ContractHarness {
    fn new() -> Self {
        // Each fixture starts a real process and exercises OS-level writer
        // locks. Serializing fixtures avoids test-runner process churn from
        // obscuring those boundaries; clients within a fixture stay concurrent.
        static FIXTURE_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        let serial = FIXTURE_LOCK
            .get_or_init(|| Mutex::new(()))
            .lock()
            .expect("fixture lock poisoned");
        let root = tempfile::tempdir().expect("temporary host root");
        let socket = root.path().join("castord.sock");
        // The daemon's safe-bind algorithm must remove a stale inode without
        // touching an active listener.
        drop(UnixListener::bind(&socket).expect("create stale socket inode"));
        let daemon = Command::new(env!("CARGO_BIN_EXE_castord"))
            .args([
                "--storage-root",
                root.path().to_str().unwrap(),
                "--socket",
                socket.to_str().unwrap(),
            ])
            .spawn()
            .expect("launch castord");
        let deadline = Instant::now() + Duration::from_secs(3);
        while UnixStream::connect(&socket).is_err() {
            assert!(
                Instant::now() < deadline,
                "castord must listen within startup timeout"
            );
            thread::sleep(Duration::from_millis(10));
        }
        // Let the daemon accept and retire the readiness probe before a
        // long-lived stop-and-wait client starts its first frame.
        thread::sleep(Duration::from_millis(25));
        Self {
            _serial: serial,
            root,
            socket,
            daemon: Mutex::new(daemon),
        }
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
        let response = call(
            &mut self.client(),
            "provider-count",
            "__ProviderSubmissionCount",
            json!({}),
        );
        response.outcome.unwrap()["count"].as_u64().unwrap() as usize
    }

    fn lose_adapter_dedup_state(&self) {
        let response = call(
            &mut self.client(),
            "lose-dedup",
            "__LoseAdapterDedupState",
            json!({}),
        );
        assert_outcome(response, "AdapterDedupLost");
    }
}

impl Drop for ContractHarness {
    fn drop(&mut self) {
        let mut daemon = self.daemon.lock().expect("daemon mutex poisoned");
        let _ = daemon.kill();
        let _ = daemon.wait();
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
    assert_eq!(
        response
            .outcome
            .as_ref()
            .and_then(|outcome| outcome.get("type")),
        Some(&json!(expected))
    );
}

fn assert_attempt_armed(response: SyscallResponse, attempt_id: u64) {
    assert_eq!(
        response.outcome,
        Some(json!({ "type": "AttemptArmed", "attempt_id": attempt_id }))
    );
}

fn commit_ready_turn(client: &mut GatewayClient, action_manifest: &[&str]) {
    let manifest_content = format!("{}\n", action_manifest.join("\n")).into_bytes();
    let manifest_digest = format!("sha256:{:x}", Sha256::digest(&manifest_content));
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
            json!({ "region_ref": "region://manifest", "content_digest": manifest_digest, "content": manifest_content, "profile": "D1" }),
        ),
        "Success",
    );
    assert_outcome(
        call(
            client,
            "commit",
            "CommitTurn",
            json!({ "lease_epoch": 1, "base_projection_digest": DIGEST, "successor_region_id": "region://observation", "successor_digest": DIGEST, "action_manifest_region_id": "region://manifest", "action_manifest_digest": manifest_digest, "action_manifest": action_manifest }),
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
    let other_root = tempfile::tempdir().expect("temporary child runtime root");
    let other_socket = other_root.path().join("child-runtime.sock");
    let status_file = other_root.path().join("child-runtime.status");
    let child = format!(
        "python3 -c '{}' ; echo $? > {}",
        r#"import json, os, socket, struct
def recv_exact(sock, size):
    chunks = []
    while size:
        chunk = sock.recv(size)
        assert chunk
        chunks.append(chunk)
        size -= len(chunk)
    return bytes().join(chunks)

s = socket.socket(socket.AF_UNIX)
s.connect(os.environ["CASTOR_IPC_SOCKET"])
request = {"request_id": "child-turn", "op": "AdmitTurn", "payload": {"agent_id": "test-agent", "turn_id": 1, "lease_epoch": 0, "base_projection_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}}
body = json.dumps(request, separators=(",", ":")).encode()
s.sendall(struct.pack(">I", len(body)) + body)
size = struct.unpack(">I", recv_exact(s, 4))[0]
response = json.loads(recv_exact(s, size))
assert response["outcome"]["type"] == "Admitted""#,
        status_file.display(),
    );
    let mut daemon = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            other_root.path().to_str().unwrap(),
            "--socket",
            other_socket.to_str().unwrap(),
            "--child",
            &child,
        ])
        .spawn()
        .expect("launch castord with reference runtime child");
    let deadline = Instant::now() + Duration::from_secs(3);
    while !status_file.exists() {
        assert!(
            Instant::now() < deadline,
            "reference runtime child must complete within startup timeout"
        );
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(
        fs::read_to_string(&status_file)
            .expect("read reference runtime exit status")
            .trim(),
        "0",
        "reference runtime child must exit cleanly after admission"
    );
    let _ = daemon.kill();
    let _ = daemon.wait();
    drop(harness);
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
    let other_root = tempfile::tempdir().expect("separate colliding daemon root");
    let socket_metadata_before = fs::metadata(&harness.socket).expect("active socket inode");
    let output = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            other_root.path().to_str().unwrap(),
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
    assert_outcome(
        call(
            &mut harness.client(),
            "active-daemon-still-responds",
            "__ProviderSubmissionCount",
            json!({}),
        ),
        "ProviderSubmissionCount",
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
    let mut oversized = UnixStream::connect(&harness.socket).expect("connect oversized probe");
    oversized
        .write_all(&((MAX_FRAME_BYTES as u32 + 1024).to_be_bytes()))
        .expect("send oversized frame header");
    let oversized_response: SyscallResponse = serde_json::from_slice(
        &read_framed(&mut oversized).expect("read oversized-frame rejection response"),
    )
    .expect("decode oversized-frame rejection response");
    assert_eq!(oversized_response.error.unwrap().code, "MalformedRequest");
    assert!(
        matches!(read_framed(&mut oversized), Err(error) if error.kind() == ErrorKind::UnexpectedEof)
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
    let other_root = tempfile::tempdir().expect("temporary supervisor root");
    let other_socket = other_root.path().join("supervisor.sock");
    let pid_file = other_root.path().join("supervised-child.pid");
    let child = format!("echo $$ > {}; exec sleep 60", pid_file.display());
    let mut daemon = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            other_root.path().to_str().unwrap(),
            "--socket",
            other_socket.to_str().unwrap(),
            "--child",
            &child,
        ])
        .spawn()
        .expect("launch supervised castord");
    let deadline = Instant::now() + Duration::from_secs(3);
    while UnixStream::connect(&other_socket).is_err() || !pid_file.exists() {
        assert!(Instant::now() < deadline, "supervised daemon must start");
        thread::sleep(Duration::from_millis(10));
    }
    let child_pid = fs::read_to_string(&pid_file)
        .expect("read supervised child pid")
        .trim()
        .parse::<u32>()
        .expect("child pid must be numeric");
    let mut client = GatewayClient::connect(&other_socket).expect("connect supervised daemon");
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
    assert_eq!(
        call(
            &mut client,
            "fence",
            "PersistFence",
            json!({ "generation": 2 })
        )
        .outcome,
        Some(json!({ "type": "GenerationFenced", "generation": 2 }))
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let output = Command::new("ps")
            .args(["-p", &child_pid.to_string(), "-o", "stat="])
            .output()
            .expect("inspect supervised child");
        if output.stdout.is_empty() {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "supervisor must kill and reap child"
        );
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(
        call(&mut client, "stale-certificate", "PresentAdmissionCertificate", json!({ "action_id": "action-1", "target_scope": "scope-1", "capability_id": "capability-1", "generation": 1 })).outcome,
        Some(json!({ "type": "RejectedStaleGeneration", "current_generation": 2 }))
    );
    let _ = daemon.kill();
    let _ = daemon.wait();
    drop(harness);
}
