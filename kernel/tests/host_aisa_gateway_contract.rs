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
use castor_kernel::{
    c01_storage::CoreEntry,
    c06_composition::{AcquireDispatchRequest, D1GovernedTurnAuthority},
};
use hmac::{Hmac, Mac};
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
const EVIDENCE_KEY: &[u8] = b"host-contract-evidence-key-32bytes";

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

struct ContractHarness {
    _serial: MutexGuard<'static, ()>,
    root: TempDir,
    socket: PathBuf,
    control_socket: PathBuf,
    evidence_socket: PathBuf,
    actuator_socket: PathBuf,
    daemon: Mutex<Child>,
}

impl ContractHarness {
    fn new() -> Self {
        Self::start(true)
    }

    fn without_test_opcodes() -> Self {
        Self::start(false)
    }

    fn start(allow_test_opcodes: bool) -> Self {
        Self::start_with_fault(allow_test_opcodes, None)
    }

    fn start_with_fault(allow_test_opcodes: bool, fault_point: Option<&str>) -> Self {
        Self::start_with_policy(allow_test_opcodes, fault_point, false)
    }

    fn with_workspace_scope() -> Self {
        Self::start_with_policy(false, None, true)
    }

    fn start_with_policy(
        allow_test_opcodes: bool,
        fault_point: Option<&str>,
        allow_workspace_scope: bool,
    ) -> Self {
        // Each fixture starts a real process and exercises OS-level writer
        // locks. Serializing fixtures avoids test-runner process churn from
        // obscuring those boundaries; clients within a fixture stay concurrent.
        static FIXTURE_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        let serial = FIXTURE_LOCK
            .get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let root = tempfile::tempdir().expect("temporary host root");
        let socket = root.path().join("castord.sock");
        let control_socket = root.path().join("control.sock");
        let evidence_socket = root.path().join("evidence.sock");
        let actuator_socket = root.path().join("actuator.sock");
        let trust_config = root.path().join("evidence-trust.json");
        let actuator_trust_config = root.path().join("actuator-trust.json");
        fs::write(
            &trust_config,
            serde_json::to_vec(&json!({
                "issuer": "host-contract-evidence-service",
                "peer_uid": fs::metadata(root.path()).expect("root metadata").uid(),
                "adapter_id": "c04:generic",
                "receipt_algorithm": "HMAC-SHA256",
                "key_hex": hex_encode(EVIDENCE_KEY),
                "canonical_scopes": if allow_workspace_scope { json!({}) } else { json!({
                    "action-1": "scope-1",
                    "action-2": "scope-1"
                }) },
                "allowed_scope_prefixes": if allow_workspace_scope { vec!["workspace:"] } else { vec![] }
            }))
            .expect("serialize evidence trust config"),
        )
        .expect("write evidence trust config");
        fs::write(
            &actuator_trust_config,
            serde_json::to_vec(&json!({
                "peer_uid": fs::metadata(root.path()).expect("root metadata").uid(),
                "actuator_id": "c04:generic"
            }))
            .expect("serialize actuator trust config"),
        )
        .expect("write actuator trust config");
        // The daemon's safe-bind algorithm must remove a stale inode without
        // touching an active listener.
        drop(UnixListener::bind(&socket).expect("create stale socket inode"));
        let mut command = Command::new(env!("CARGO_BIN_EXE_castord"));
        command.args([
            "--storage-root",
            root.path().to_str().unwrap(),
            "--socket",
            socket.to_str().unwrap(),
            "--control-socket",
            control_socket.to_str().unwrap(),
            "--evidence-socket",
            evidence_socket.to_str().unwrap(),
            "--actuator-socket",
            actuator_socket.to_str().unwrap(),
        ]);
        command.env("CASTORD_EVIDENCE_TRUST_CONFIG", &trust_config);
        command.env("CASTORD_ACTUATOR_TRUST_CONFIG", &actuator_trust_config);
        if let Some(fault_point) = fault_point {
            command.env("CASTORD_TEST_FAULT_POINT", fault_point);
        }
        if allow_test_opcodes {
            command.arg("--allow-test-opcodes");
        }
        let daemon = command.spawn().expect("launch castord");
        let deadline = Instant::now() + Duration::from_secs(3);
        while UnixStream::connect(&socket).is_err()
            || UnixStream::connect(&control_socket).is_err()
            || UnixStream::connect(&evidence_socket).is_err()
            || UnixStream::connect(&actuator_socket).is_err()
        {
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
            control_socket,
            evidence_socket,
            actuator_socket,
            daemon: Mutex::new(daemon),
        }
    }

    fn client(&self) -> GatewayClient {
        GatewayClient::connect(&self.socket).expect(
            "T-302-C castord daemon is intentionally absent: this RED contract must fail at socket connect",
        )
    }

    fn control_client(&self) -> GatewayClient {
        GatewayClient::connect(&self.control_socket)
            .expect("control socket must accept host client")
    }

    fn evidence_client(&self) -> GatewayClient {
        GatewayClient::connect(&self.evidence_socket)
            .expect("evidence socket must accept the configured trusted client")
    }

    fn actuator_client(&self) -> GatewayClient {
        GatewayClient::connect(&self.actuator_socket)
            .expect("actuator socket must accept the configured trusted client")
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

    fn wait_for_daemon_exit_or_kill(&self) -> std::process::ExitStatus {
        let mut daemon = self.daemon.lock().expect("daemon mutex poisoned");
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            if let Some(status) = daemon.try_wait().expect("poll daemon") {
                return status;
            }
            if Instant::now() >= deadline {
                let _ = daemon.kill();
                return daemon.wait().expect("reap noncrashing daemon");
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}

#[test]
fn trusted_workspace_scope_policy_allows_only_workspace_actions() {
    let harness = ContractHarness::with_workspace_scope();
    let mut agent = harness.client();
    commit_ready_turn(
        &mut agent,
        &["action-1", "action-2"],
        Some(&mut harness.control_client()),
    );
    assert_outcome(
        call(
            &mut agent,
            "workspace-register",
            "RegisterAction",
            json!({
                "action_id": "action-1",
                "stable_operation_id": "dispatch-1",
                "action_family": "c04:generic",
                "target_scope": "workspace:src/lib.rs"
            }),
        ),
        "ActionRegistered",
    );
    assert_attempt_armed(
        call(
            &mut agent,
            "workspace-arm",
            "PresentAdmissionCertificate",
            json!({
                "action_id": "action-1",
                "target_scope": "workspace:src/lib.rs",
                "capability_id": "capability-1",
                "generation": 1
            }),
        ),
        1,
    );
    assert_outcome(
        call(
            &mut agent,
            "outside-register",
            "RegisterAction",
            json!({
                "action_id": "action-2",
                "stable_operation_id": "dispatch-2",
                "action_family": "c04:generic",
                "target_scope": "host:/etc/passwd"
            }),
        ),
        "RejectedPrecondition",
    );
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

fn acquire_action(
    harness: &ContractHarness,
    request_id: &str,
    attempt_id: u64,
    dispatch_identity: &str,
) -> SyscallResponse {
    call(
        &mut harness.actuator_client(),
        request_id,
        "AcquireDispatch",
        json!({
            "attempt_id": attempt_id,
            "dispatch_identity": dispatch_identity,
            "actuator_id": "c04:generic"
        }),
    )
}

fn assert_delivery_envelope(response: SyscallResponse, expected: &str) {
    assert_eq!(response.status, "Ok");
    assert_eq!(
        response.outcome.expect("delivery envelope")["delivery_outcome"],
        json!(expected)
    );
}

fn commit_ready_turn(
    client: &mut GatewayClient,
    action_manifest: &[&str],
    reporter: Option<&mut GatewayClient>,
) {
    let manifest_content = format!("{}\n", action_manifest.join("\n")).into_bytes();
    let manifest_digest = format!("sha256:{:x}", Sha256::digest(&manifest_content));
    let mut action_bindings = Vec::new();
    for action_id in action_manifest {
        let payload = format!("payload-{action_id}").into_bytes();
        let payload_digest = format!("sha256:{:x}", Sha256::digest(&payload));
        let payload_region_ref = format!("region://payload/{action_id}");
        assert_outcome(
            call(
                client,
                &format!("ensure-payload-{action_id}"),
                "EnsureRegion",
                json!({
                    "region_ref": payload_region_ref,
                    "content_digest": payload_digest,
                    "content": payload,
                    "profile": "D1"
                }),
            ),
            "Success",
        );
        action_bindings.push(json!({
            "action_id": action_id,
            "payload_region_ref": payload_region_ref,
            "payload_digest": payload_digest,
            "actuator_id": "c04:generic"
        }));
    }
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
            json!({ "agent_id": "agent-1", "turn_id": 1, "lease_epoch": 0, "base_projection_digest": DIGEST, "expected_generation": 1 }),
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
    {
        let report_client = reporter.unwrap_or(&mut *client);
        assert_outcome(
            call(
                report_client,
                "report",
                "ReportOutcome",
                json!({ "interaction_id": "interaction-1", "observation_region_id": "region://observation", "observation_digest": DIGEST }),
            ),
            "InteractionBound",
        );
    }
    let consumed = call(
        client,
        "consume",
        "ConsumeInteraction",
        json!({ "interaction_id": "interaction-1", "lease_epoch": 1 }),
    );
    assert_eq!(consumed.status, "Ok");
    assert_eq!(
        consumed.outcome,
        Some(json!({
            "type": "InteractionConsumed",
            "payload": {
                "interaction_id": "interaction-1",
                "observation_region_id": "region://observation",
                "observation_digest": DIGEST,
                "content": [],
                "lease_epoch": 1
            }
        }))
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
            json!({ "lease_epoch": 1, "base_projection_digest": DIGEST, "successor_region_id": "region://observation", "successor_digest": DIGEST, "action_manifest_region_id": "region://manifest", "action_manifest_digest": manifest_digest, "action_manifest": action_manifest, "action_bindings": action_bindings }),
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
            json!({
                "action_id": action_id,
                "stable_operation_id": if action_id == "action-1" { "dispatch-1" } else { "dispatch-2" },
                "action_family": "c04:generic",
                "target_scope": scope
            }),
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
    commit_ready_turn(&mut client, &["action-1"], None);
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
    assert_delivery_envelope(
        acquire_action(&harness, "deliver", 1, "dispatch-1"),
        "Delivered",
    );
    let mut receipt = json!({
        "attempt_id": 1,
        "stable_operation_id": "dispatch-1",
        "request_digest": "scope-1",
        "issuer": "host-contract-evidence-service",
        "adapter_id": "c04:generic",
        "settlement_schema_version": 1,
        "resolution": "Confirmed",
        "actuator_state": "Committed"
    });
    let receipt_bytes = serde_json::to_vec(&receipt).expect("serialize receipt");
    let mut mac = Hmac::<Sha256>::new_from_slice(EVIDENCE_KEY).expect("HMAC key");
    mac.update(&receipt_bytes);
    receipt["signature"] = json!(hex_encode(&mac.finalize().into_bytes()));
    let evidence_bytes = serde_json::to_vec(&receipt).expect("serialize signed receipt");
    let evidence_digest = format!("sha256:{:x}", Sha256::digest(&evidence_bytes));
    assert_outcome(
        call(
            &mut client,
            "ensure-evidence",
            "EnsureRegion",
            json!({
                "region_ref": "region://settlement-receipt",
                "content_digest": evidence_digest,
                "content": evidence_bytes,
                "profile": "D1"
            }),
        ),
        "Success",
    );
    let mut settlement = receipt;
    settlement["dispatch_identity"] = json!("dispatch-1");
    settlement["evidence_region_id"] = json!("region://settlement-receipt");
    settlement["evidence_digest"] = json!(evidence_digest);
    settlement["proof_class"] = json!("ProviderConfirmation");
    assert_outcome(
        call(
            &mut harness.evidence_client(),
            "settle",
            "PresentSettlementCertificate",
            settlement,
        ),
        "Settled",
    );
    assert_outcome(
        call(
            &mut client,
            "rearm",
            "PresentAdmissionCertificate",
            json!({ "action_id": "action-1", "target_scope": "scope-1", "capability_id": "capability-1", "generation": 1 }),
        ),
        "RejectedCurrentState",
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
    let status = loop {
        match fs::read_to_string(&status_file) {
            Ok(status) if !status.trim().is_empty() => break status,
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => panic!("read reference runtime status: {error}"),
        }
        assert!(
            Instant::now() < deadline,
            "reference runtime child must complete within startup timeout"
        );
        thread::sleep(Duration::from_millis(10));
    };
    assert_eq!(
        status.trim(),
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
            json!({ "action_id": "action-1", "action_family": "c04:generic" }),
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
    commit_ready_turn(&mut client, &["action-1"], None);
    assert_outcome(
        call(
            &mut client,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-2", "action_family": "c04:generic" }),
        ),
        "RejectedPrecondition",
    );
}

#[test]
fn scenario_06_deliver_without_record_is_rejected_without_provider_submission() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"], None);
    arm_action(&mut client, "action-1", "scope-1");
    assert_outcome(
        acquire_action(&harness, "deliver", 1, "dispatch-1"),
        "RejectedCurrentState",
    );
    assert_eq!(harness.provider_submission_count(), 0);
}

#[test]
fn scenario_07_duplicate_deliver_is_deduplicated_over_socket() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"], None);
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
    assert_delivery_envelope(
        acquire_action(&harness, "deliver-1", 1, "dispatch-1"),
        "Delivered",
    );
    assert_delivery_envelope(
        acquire_action(&harness, "deliver-2", 1, "dispatch-1"),
        "DuplicateDelivery",
    );
    assert_eq!(harness.provider_submission_count(), 1);
}

#[test]
fn scenario_08_missing_adapter_dedup_after_dispatch_is_ambiguous() {
    let harness = ContractHarness::new();
    let mut client = harness.client();
    commit_ready_turn(&mut client, &["action-1"], None);
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
    let response = call(
        &mut harness.actuator_client(),
        "acquire-ambiguous",
        "AcquireDispatch",
        json!({
            "attempt_id": 1,
            "dispatch_identity": "dispatch-1",
            "actuator_id": "c04:generic"
        }),
    );
    assert_eq!(response.status, "Ok");
    assert_eq!(response.outcome, Some(json!({ "type": "Ambiguous" })));
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
    commit_ready_turn(&mut client, &["action-1"], None);
    assert_outcome(
        call(
            &mut client,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-1", "action_family": "c04:generic" }),
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
    commit_ready_turn(&mut client_one, &["action-1", "action-2"], None);
    arm_action(&mut client_one, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut client_two,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-2", "action_family": "c04:generic" }),
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
fn malformed_action_binding_is_rejected_at_gateway_boundary() {
    let harness = ContractHarness::new();
    let response = call(
        &mut harness.client(),
        "malformed-action-binding",
        "CommitTurn",
        json!({
            "lease_epoch": 1,
            "base_projection_digest": DIGEST,
            "successor_region_id": "region://successor",
            "successor_digest": DIGEST,
            "action_manifest_region_id": "region://manifest",
            "action_manifest_digest": DIGEST,
            "action_manifest": ["action-1"],
            "action_bindings": [{
                "action_id": "action-1",
                "payload_region_ref": "region://payload/action-1",
                "payload_digest": DIGEST
            }]
        }),
    );
    assert_eq!(response.status, "Error");
    let error = response.error.expect("malformed binding error");
    assert_eq!(error.code, "MalformedRequest");
    assert!(error.message.contains("invalid action_bindings"));
}

#[test]
fn consume_interaction_rejects_arbitrary_region_selector_at_gateway_boundary() {
    let harness = ContractHarness::new();
    let response = call(
        &mut harness.client(),
        "consume-region-selector",
        "ConsumeInteraction",
        json!({
            "interaction_id": "interaction-1",
            "lease_epoch": 1,
            "observation_region_id": "region://attacker-selected"
        }),
    );
    assert_eq!(response.status, "Error");
    assert_eq!(
        response.error.expect("strict request error").code,
        "MalformedRequest"
    );
}

#[test]
fn scenario_17_lost_ack_after_commit_does_not_mint_a_second_turn() {
    let harness = ContractHarness::new();
    let mut first_client = harness.client();
    commit_ready_turn(&mut first_client, &["action-1"], None);
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
    let other_control_socket = other_root.path().join("supervisor-control.sock");
    let pid_file = other_root.path().join("supervised-child.pid");
    let child = format!("echo $$ > {}; exec sleep 60", pid_file.display());
    let mut daemon = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            other_root.path().to_str().unwrap(),
            "--socket",
            other_socket.to_str().unwrap(),
            "--control-socket",
            other_control_socket.to_str().unwrap(),
            "--child",
            &child,
        ])
        .spawn()
        .expect("launch supervised castord");
    let deadline = Instant::now() + Duration::from_secs(3);
    while UnixStream::connect(&other_socket).is_err()
        || UnixStream::connect(&other_control_socket).is_err()
        || !pid_file.exists()
    {
        assert!(Instant::now() < deadline, "supervised daemon must start");
        thread::sleep(Duration::from_millis(10));
    }
    let child_pid = fs::read_to_string(&pid_file)
        .expect("read supervised child pid")
        .trim()
        .parse::<u32>()
        .expect("child pid must be numeric");
    let mut client = GatewayClient::connect(&other_socket).expect("connect supervised daemon");
    let mut control = GatewayClient::connect(&other_control_socket).expect("connect host control");
    commit_ready_turn(&mut client, &["action-1"], Some(&mut control));
    assert_outcome(
        call(
            &mut client,
            "register",
            "RegisterAction",
            json!({ "action_id": "action-1", "action_family": "c04:generic" }),
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

#[test]
fn scenario_19_dual_socket_uses_closed_channel_allowlists() {
    let harness = ContractHarness::new();
    let agent_error = call(
        &mut harness.client(),
        "agent-grant",
        "GrantCapability",
        json!({}),
    );
    assert_eq!(agent_error.status, "Error");
    assert_eq!(agent_error.error.unwrap().code, "UnauthorizedOpcode");

    let control_error = call(
        &mut harness.control_client(),
        "control-admit",
        "AdmitTurn",
        json!({}),
    );
    assert_eq!(control_error.status, "Error");
    assert_eq!(control_error.error.unwrap().code, "UnauthorizedOpcode");

    let summary = call(
        &mut harness.control_client(),
        "control-summary",
        "GetProjectionSummary",
        json!({}),
    );
    assert_eq!(summary.status, "Ok");
    assert_eq!(summary.outcome.unwrap()["generation"], json!(1));
}

#[test]
fn agent_observe_projection_is_read_only_and_tracks_host_projection_updates() {
    let harness = ContractHarness::without_test_opcodes();
    let before = call(
        &mut harness.client(),
        "observe-before",
        "ObserveProjection",
        json!({}),
    );
    assert_eq!(before.status, "Ok");
    assert_eq!(
        before.outcome.as_ref().unwrap()["type"],
        "ProjectionObserved"
    );
    assert_eq!(before.outcome.as_ref().unwrap()["generation"], 1);
    assert!(before.outcome.as_ref().unwrap()["projection_digest"].is_null());
    let initial_journal = call(
        &mut harness.control_client(),
        "journal-before",
        "InspectJournal",
        json!({}),
    )
    .outcome
    .unwrap()["entries"]
        .as_array()
        .unwrap()
        .len();
    let mut agent = harness.client();
    commit_ready_turn(
        &mut agent,
        &["action-1"],
        Some(&mut harness.control_client()),
    );
    arm_action(&mut agent, "action-1", "scope-1");
    let before_second_read = call(
        &mut harness.control_client(),
        "journal-before-second-read",
        "InspectJournal",
        json!({}),
    )
    .outcome
    .unwrap()["entries"]
        .as_array()
        .unwrap()
        .len();
    let after = call(&mut agent, "observe-after", "ObserveProjection", json!({}));
    assert_eq!(after.status, "Ok");
    assert_eq!(after.outcome.as_ref().unwrap()["generation"], 1);
    assert_ne!(
        after.outcome.as_ref().unwrap()["projection_digest"],
        before.outcome.as_ref().unwrap()["projection_digest"]
    );
    let final_journal = call(
        &mut harness.control_client(),
        "journal-after",
        "InspectJournal",
        json!({}),
    )
    .outcome
    .unwrap()["entries"]
        .as_array()
        .unwrap()
        .len();
    assert_eq!(final_journal, before_second_read);
    assert!(final_journal > initial_journal);

    assert_eq!(
        call(
            &mut harness.control_client(),
            "fence-host",
            "PersistFence",
            json!({ "generation": 2 }),
        )
        .outcome
        .unwrap()["type"],
        "GenerationFenced"
    );
    let fenced_journal_size = call(
        &mut harness.control_client(),
        "journal-before-fenced-admit",
        "InspectJournal",
        json!({}),
    )
    .outcome
    .unwrap()["entries"]
        .as_array()
        .unwrap()
        .len();
    let stale_generation_admit = call(
        &mut agent,
        "fenced-admit",
        "AdmitTurn",
        json!({
            "agent_id": "agent-1",
            "turn_id": 2,
            "lease_epoch": 0,
            "base_projection_digest": after.outcome.as_ref().unwrap()["projection_digest"],
            "expected_generation": 1
        }),
    );
    assert_eq!(
        stale_generation_admit.outcome,
        Some(json!({ "type": "RejectedStaleGeneration", "current_generation": 2 }))
    );
    let fenced_journal_after = call(
        &mut harness.control_client(),
        "journal-after-fenced-admit",
        "InspectJournal",
        json!({}),
    )
    .outcome
    .unwrap()["entries"]
        .as_array()
        .unwrap()
        .len();
    assert_eq!(fenced_journal_after, fenced_journal_size);
    let fenced = call(&mut agent, "observe-fenced", "ObserveProjection", json!({}));
    assert_eq!(fenced.outcome.as_ref().unwrap()["generation"], 2);
    let control_observation = call(
        &mut harness.control_client(),
        "control-observe-fenced",
        "ObserveProjection",
        json!({}),
    );
    assert_eq!(control_observation.status, "Ok");
    assert_eq!(control_observation.outcome, fenced.outcome);

    let forbidden_summary = call(
        &mut agent,
        "agent-summary",
        "GetProjectionSummary",
        json!({}),
    );
    assert_eq!(forbidden_summary.status, "Error");
    assert_eq!(forbidden_summary.error.unwrap().code, "UnauthorizedOpcode");
}

#[test]
fn model_request_region_is_readable_only_by_trusted_control() {
    let harness = ContractHarness::without_test_opcodes();
    let mut agent = harness.client();
    let mut control = harness.control_client();
    let bytes = br#"{"messages":[{"role":"user","content":"repair"}],"tools":[]}"#;
    let digest = format!("sha256:{:x}", Sha256::digest(bytes));
    assert_outcome(
        call(
            &mut agent,
            "persist-model-request",
            "EnsureRegion",
            json!({
                "region_ref": "region://model-request/interaction-1",
                "content_digest": digest,
                "content": bytes.as_slice(),
                "profile": "D1"
            }),
        ),
        "Success",
    );
    let denied = call(
        &mut agent,
        "guest-read-model-request",
        "ReadModelRequest",
        json!({"interaction_id": "interaction-1"}),
    );
    assert_eq!(denied.status, "Error");
    assert_eq!(denied.error.unwrap().code, "UnauthorizedOpcode");
    let read = call(
        &mut control,
        "host-read-model-request",
        "ReadModelRequest",
        json!({"interaction_id": "interaction-1"}),
    );
    assert_eq!(read.status, "Ok");
    let content = read.outcome.expect("immutable request content");
    assert_eq!(
        content["region_ref"],
        "region://model-request/interaction-1"
    );
    assert_eq!(content["content_digest"], digest);
    assert_eq!(content["content"], json!(bytes.as_slice()));
}

#[test]
fn agent_channel_cannot_bind_its_own_model_observation() {
    let harness = ContractHarness::without_test_opcodes();
    let mut agent = harness.client();
    assert_outcome(
        call(
            &mut agent,
            "admit-hostile-model-turn",
            "AdmitTurn",
            json!({
                "agent_id": "agent-1",
                "turn_id": 1,
                "lease_epoch": 0,
                "base_projection_digest": DIGEST
            }),
        ),
        "Admitted",
    );
    assert_outcome(
        call(
            &mut agent,
            "request-hostile-model",
            "RequestInteraction",
            json!({
                "interaction_id": "interaction-hostile",
                "lease_epoch": 0,
                "request_digest": DIGEST
            }),
        ),
        "InteractionRequested",
    );
    let before = call(
        &mut harness.control_client(),
        "journal-before-hostile-report",
        "InspectJournal",
        json!({}),
    )
    .outcome;
    let response = call(
        &mut agent,
        "agent-forged-report",
        "ReportOutcome",
        json!({
            "interaction_id": "interaction-hostile",
            "observation_region_id": "region://forged-observation",
            "observation_digest": DIGEST
        }),
    );
    assert_eq!(response.status, "Error");
    assert_eq!(response.error.unwrap().code, "UnauthorizedOpcode");
    let after = call(
        &mut harness.control_client(),
        "journal-after-hostile-report",
        "InspectJournal",
        json!({}),
    )
    .outcome;
    assert_eq!(after, before, "untrusted report must not append a binding");
    assert_outcome(
        call(
            &mut harness.control_client(),
            "trusted-model-region",
            "EnsureRegion",
            json!({
                "region_ref": "region://forged-observation",
                "content_digest": DIGEST,
                "content": [],
                "profile": "D1"
            }),
        ),
        "Success",
    );
    assert_outcome(
        call(
            &mut harness.control_client(),
            "trusted-model-report",
            "ReportOutcome",
            json!({
                "interaction_id": "interaction-hostile",
                "observation_region_id": "region://forged-observation",
                "observation_digest": DIGEST
            }),
        ),
        "InteractionBound",
    );
}

#[test]
fn opcode_isolation_without_test_flag() {
    let harness = ContractHarness::without_test_opcodes();
    let mut client = harness.client();

    for opcode in [
        "__LoseAdapterDedupState",
        "__ProviderSubmissionCount",
        "Replay",
    ] {
        let response = call(&mut client, opcode, opcode, json!({}));
        assert_eq!(response.status, "Error", "{opcode} must be rejected");
        assert_eq!(
            response.error.expect("unauthorized error").code,
            "UnauthorizedOpcode",
            "{opcode} must fail at the channel allowlist"
        );
    }

    assert_outcome(
        call(
            &mut client,
            "normal-admit",
            "AdmitTurn",
            json!({
                "agent_id": "production-agent",
                "turn_id": 1,
                "lease_epoch": 0,
                "base_projection_digest": DIGEST
            }),
        ),
        "Admitted",
    );
}

#[test]
fn scenario_20_actuator_socket_is_closed_and_returns_only_bound_payload() {
    let harness = ContractHarness::new();
    let mut agent = harness.client();
    commit_ready_turn(&mut agent, &["action-1"], None);
    arm_action(&mut agent, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut agent,
            "record-dispatch",
            "RecordDispatchAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "DispatchRecorded",
    );

    let guest_delivery = call(
        &mut agent,
        "guest-delivery-preemption",
        "DeliverArmedAttempt",
        json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
    );
    assert_eq!(guest_delivery.status, "Error");
    assert_eq!(
        guest_delivery
            .error
            .expect("closed legacy delivery error")
            .code,
        "UnauthorizedOpcode"
    );

    for (name, mut client) in [
        ("agent", harness.client()),
        ("control", harness.control_client()),
        ("evidence", harness.evidence_client()),
    ] {
        let response = call(
            &mut client,
            &format!("{name}-acquire"),
            "AcquireDispatch",
            json!({
                "attempt_id": 1,
                "dispatch_identity": "dispatch-1",
                "actuator_id": "c04:generic"
            }),
        );
        assert_eq!(response.status, "Error");
        assert_eq!(
            response.error.expect("closed channel error").code,
            "UnauthorizedOpcode"
        );
    }

    let mut actuator = harness.actuator_client();
    let first = call(
        &mut actuator,
        "actuator-acquire",
        "AcquireDispatch",
        json!({
            "attempt_id": 1,
            "dispatch_identity": "dispatch-1",
            "actuator_id": "c04:generic"
        }),
    );
    assert_eq!(first.status, "Ok");
    let first = first.outcome.expect("delivered payload envelope");
    assert_eq!(first["delivery_outcome"], json!("Delivered"));
    assert_eq!(first["action_id"], json!("action-1"));
    assert_eq!(
        first["payload_region_ref"],
        json!("region://payload/action-1")
    );
    assert_eq!(first["payload"], json!(b"payload-action-1"));

    let duplicate = call(
        &mut actuator,
        "actuator-acquire-duplicate",
        "AcquireDispatch",
        json!({
            "attempt_id": 1,
            "dispatch_identity": "dispatch-1",
            "actuator_id": "c04:generic"
        }),
    );
    assert_eq!(duplicate.status, "Ok");
    let duplicate = duplicate.outcome.expect("duplicate payload envelope");
    assert_eq!(duplicate["delivery_outcome"], json!("DuplicateDelivery"));
    assert_eq!(duplicate["payload"], first["payload"]);
    assert_eq!(duplicate["payload_digest"], first["payload_digest"]);

    let malformed = call(
        &mut actuator,
        "actuator-selector-injection",
        "AcquireDispatch",
        json!({
            "attempt_id": 1,
            "dispatch_identity": "dispatch-1",
            "actuator_id": "c04:generic",
            "payload_region_ref": "region://attacker-selected"
        }),
    );
    assert_eq!(malformed.status, "Error");
    assert_eq!(
        malformed.error.expect("strict request error").code,
        "MalformedRequest"
    );
}

fn prepare_dispatched_attempt(harness: &ContractHarness) {
    let mut agent = harness.client();
    let mut control = harness.control_client();
    commit_ready_turn(&mut agent, &["action-1"], Some(&mut control));
    arm_action(&mut agent, "action-1", "scope-1");
    assert_outcome(
        call(
            &mut agent,
            "record-dispatch",
            "RecordDispatchAttempt",
            json!({ "attempt_id": 1, "dispatch_identity": "dispatch-1" }),
        ),
        "DispatchRecorded",
    );
}

#[test]
fn scenario_21_delivery_fault_points_select_the_durable_journal_seam() {
    for (fault_point, expected_exit, submission_persisted) in [
        ("before_delivery_append", 86, false),
        ("after_delivery_fsync_before_response", 87, true),
    ] {
        let harness = ContractHarness::start_with_fault(true, Some(fault_point));
        prepare_dispatched_attempt(&harness);
        let response = harness.actuator_client().request(&request(
            "fault-acquire",
            "AcquireDispatch",
            json!({
                "attempt_id": 1,
                "dispatch_identity": "dispatch-1",
                "actuator_id": "c04:generic"
            }),
        ));
        let status = harness.wait_for_daemon_exit_or_kill();
        assert!(response.is_err(), "{fault_point} must lose the response");
        assert_eq!(
            status.code(),
            Some(expected_exit),
            "{fault_point} exit code"
        );

        let mut recovered = D1GovernedTurnAuthority::open(harness.storage_root())
            .expect("reopen authority after fault");
        let has_submission = recovered.inspect_journal().iter().any(|entry| {
            matches!(
                entry,
                CoreEntry::AdapterSubmissionRecorded { attempt_id: 1 }
            )
        });
        assert_eq!(
            has_submission, submission_persisted,
            "{fault_point} journal seam"
        );
        let envelope = recovered
            .acquire_dispatch(AcquireDispatchRequest {
                attempt_id: 1,
                dispatch_identity: "dispatch-1".into(),
                actuator_id: "c04:generic".into(),
            })
            .expect("recovered actuator acquisition");
        assert_eq!(
            envelope.delivery_outcome,
            if submission_persisted {
                "DuplicateDelivery"
            } else {
                "Delivered"
            }
        );
        assert_eq!(envelope.payload, b"payload-action-1");
    }
}

#[test]
fn scenario_22_delivery_fault_environment_is_inert_without_test_flag() {
    let harness = ContractHarness::start_with_fault(false, Some("before_delivery_append"));
    prepare_dispatched_attempt(&harness);
    let response = call(
        &mut harness.actuator_client(),
        "disabled-fault-acquire",
        "AcquireDispatch",
        json!({
            "attempt_id": 1,
            "dispatch_identity": "dispatch-1",
            "actuator_id": "c04:generic"
        }),
    );
    assert_eq!(response.status, "Ok");
    assert_eq!(
        response.outcome.expect("delivered envelope")["delivery_outcome"],
        json!("Delivered")
    );
    assert!(harness
        .daemon
        .lock()
        .expect("daemon mutex poisoned")
        .try_wait()
        .expect("poll daemon")
        .is_none());
}
