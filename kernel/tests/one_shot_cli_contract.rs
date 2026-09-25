//! T-337-C: public one-shot task contract, intentionally RED until the Rust CLI exists.
//!
//! These cases exercise the user-facing `castor run --task` boundary. External
//! model and actuator effects use local test doubles; authority and the C-01
//! journal remain real. No live model provider is invoked.

use castor_kernel::host::{read_framed, write_framed, GatewayClient, SyscallRequest};
use castor_kernel::sandbox::{build_castor_untrusted_agent_config, RocheSandboxRunner};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc,
};
use std::thread;
use std::time::Duration;

const BASE_DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const WRONG_SNAPSHOT_HASH: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";
const EMPTY_REGION_DIGEST: &str =
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

fn castor_cli() -> &'static str {
    match option_env!("CARGO_BIN_EXE_castor") {
        Some(path) => path,
        None => panic!("the Rust castor CLI binary is missing; T-337-D must provide castor run"),
    }
}

fn write_manifest(root: &Path, snapshot_path: &str) -> std::path::PathBuf {
    write_manifest_with_hash(root, snapshot_path, WRONG_SNAPSHOT_HASH)
}

fn write_manifest_with_hash(
    root: &Path,
    snapshot_path: &str,
    snapshot_hash: &str,
) -> std::path::PathBuf {
    let manifest = root.join("manifest.json");
    let body = json!({
        "task_id": "task-snapshot-gate",
        "idempotency_key": "snapshot-gate-001",
        "carrier_base_image": format!("substratum/castor-pi-carrier:v1@{BASE_DIGEST}"),
        "workspace_snapshot_path": snapshot_path,
        "workspace_snapshot_sha256": snapshot_hash,
        "task_prompt": "Repair the failing unit test.",
        "verification_command": ["cargo", "test", "--test", "gate"]
    });
    fs::write(&manifest, serde_json::to_vec(&body).unwrap()).unwrap();
    manifest
}

fn create_archive(root: &Path, name: &str) -> String {
    let source = root.join("source");
    fs::create_dir(&source).unwrap();
    fs::write(source.join("defect.txt"), b"failing fixture\n").unwrap();
    let archive = root.join(name);
    let tar = Command::new("tar")
        .args(["-cf"])
        .arg(&archive)
        .args(["-C"])
        .arg(&source)
        .arg(".")
        .output()
        .expect("create real snapshot archive");
    assert!(
        tar.status.success(),
        "tar fixture: {}",
        String::from_utf8_lossy(&tar.stderr)
    );
    format!("{:x}", Sha256::digest(fs::read(&archive).unwrap()))
}

/// Test-only external seams replace image build and the Ring-3 process. The
/// task supervisor, journal, verdict, and manifest validation remain real.
/// T-337-D must ignore these variables unless `--allow-test-opcodes` is set.
fn run_with_agent_script(
    root: &Path,
    manifest: &Path,
    script: &str,
    model_socket: Option<&Path>,
) -> Output {
    use std::os::unix::fs::PermissionsExt;

    let child = root.join("mock-agent.sh");
    fs::write(&child, format!("#!/bin/sh\n{script}\n")).unwrap();
    fs::set_permissions(&child, fs::Permissions::from_mode(0o755)).unwrap();
    let mut command = task_command(root, manifest, &child);
    if let Some(socket) = model_socket {
        command.env("CASTOR_TEST_MODEL_SOCKET", socket);
    }
    command
        .output()
        .expect("run Rust castor CLI with external test seams")
}

fn task_command(root: &Path, manifest: &Path, child: &Path) -> Command {
    let mut command = Command::new(castor_cli());
    command
        .args(["run", "--allow-test-opcodes", "--task"])
        .arg(manifest)
        .env("CASTOR_TEST_AGENT_CHILD", child)
        .env(
            "CASTOR_TEST_MOCK_CHILD_BINARY",
            std::env::current_exe().unwrap(),
        )
        .env("CASTOR_TEST_DERIVED_IMAGE_DIGEST", BASE_DIGEST)
        .env("CASTOR_TEST_TASK_STATE_ROOT", root.join("task-state"));
    command
}

struct MockModelService {
    stop: Arc<AtomicBool>,
    attempts: Arc<AtomicUsize>,
    worker: Option<thread::JoinHandle<()>>,
}

impl MockModelService {
    fn start(path: &Path, response: Option<Value>, release_marker: Option<PathBuf>) -> Self {
        let listener = UnixListener::bind(path).expect("bind local mock model service");
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let attempts = Arc::new(AtomicUsize::new(0));
        let worker_stop = stop.clone();
        let worker_attempts = attempts.clone();
        let worker = thread::spawn(move || {
            while !worker_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        worker_attempts.fetch_add(1, Ordering::SeqCst);
                        if let Some(ref response) = response {
                            stream
                                .set_read_timeout(Some(Duration::from_secs(1)))
                                .unwrap();
                            if read_framed(&mut stream).is_ok() {
                                if let Some(ref marker) = release_marker {
                                    let deadline =
                                        std::time::Instant::now() + Duration::from_secs(3);
                                    while !marker.exists() && std::time::Instant::now() < deadline {
                                        thread::sleep(Duration::from_millis(10));
                                    }
                                    if !marker.exists() {
                                        continue;
                                    }
                                }
                                let bytes = serde_json::to_vec(response).unwrap();
                                write_framed(&mut stream, &bytes).unwrap();
                            }
                        }
                        // With no response, closing simulates provider transport failure.
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("mock model listener: {error}"),
                }
            }
        });
        Self {
            stop,
            attempts,
            worker: Some(worker),
        }
    }
}

impl Drop for MockModelService {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.worker.take().unwrap().join().unwrap();
    }
}

fn buffered_model_response() -> Value {
    json!({
        "interaction_id": "interaction-1",
        "observation_region_id": "region://observation",
        "observation_digest": EMPTY_REGION_DIGEST,
        "content": [],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    })
}

#[test]
fn image_build_failure_returns_truthful_failed_result() {
    use std::os::unix::fs::PermissionsExt;

    let root = tempfile::tempdir().unwrap();
    let archive_hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &archive_hash);

    let fake_bin = root.path().join("bin");
    fs::create_dir(&fake_bin).unwrap();
    let docker = fake_bin.join("docker");
    fs::write(
        &docker,
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CASTOR_TEST_DOCKER_CALLS\"\nexit 67\n",
    )
    .unwrap();
    fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
    let calls = root.path().join("docker-calls.txt");
    let path = format!("{}:{}", fake_bin.display(), std::env::var("PATH").unwrap());
    let output = Command::new(castor_cli())
        .args(["run", "--task"])
        .arg(&manifest)
        .env("PATH", path)
        .env("CASTOR_TEST_DOCKER_CALLS", &calls)
        .output()
        .expect("run Rust castor CLI");
    assert!(
        !output.status.success(),
        "failed image build must fail task"
    );
    let result = result(&output);
    assert_eq!(result["status"], "FAILED");
    assert_eq!(result["failure_reason"], "PROVISIONING_IMAGE_BUILD_FAILED");
    assert_eq!(result["workspace_snapshot_sha256"], archive_hash);
    assert_eq!(result["committed_turns"], json!([]));
    assert_eq!(result["settled_actions_count"], 0);
    assert!(result.get("derived_task_image_digest").is_none());
    assert!(result.get("test_exit_code").is_none());
    assert!(
        fs::read_to_string(&calls)
            .unwrap_or_default()
            .contains("build"),
        "the injected image builder must be attempted before reporting build failure"
    );
}

#[test]
fn symlink_inside_verified_archive_is_rejected_before_image_build() {
    use std::os::unix::fs::{symlink, PermissionsExt};

    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    fs::create_dir(&source).unwrap();
    fs::write(source.join("defect.txt"), b"failing fixture\n").unwrap();
    symlink("../outside.txt", source.join("escape")).unwrap();
    let archive = root.path().join("snapshot.tar");
    let tar_output = Command::new("tar")
        .arg("-cf")
        .arg(&archive)
        .arg("-C")
        .arg(&source)
        .arg(".")
        .output()
        .unwrap();
    assert!(tar_output.status.success());
    let bytes = fs::read(&archive).unwrap();
    let mut archive_check = tar::Archive::new(bytes.as_slice());
    assert!(
        archive_check.entries().unwrap().any(|entry| entry
            .unwrap()
            .header()
            .entry_type()
            .is_symlink()),
        "fixture must actually contain a symlink entry"
    );
    let hash = format!("{:x}", Sha256::digest(&bytes));
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let fake_bin = root.path().join("bin");
    fs::create_dir(&fake_bin).unwrap();
    let docker = fake_bin.join("docker");
    fs::write(
        &docker,
        "#!/bin/sh\nprintf 'called\\n' >> \"$CASTOR_TEST_DOCKER_CALLS\"\nexit 67\n",
    )
    .unwrap();
    fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
    let calls = root.path().join("docker-calls.txt");
    let path = format!("{}:{}", fake_bin.display(), std::env::var("PATH").unwrap());
    let output = Command::new(castor_cli())
        .args(["run", "--task"])
        .arg(&manifest)
        .env("PATH", path)
        .env("CASTOR_TEST_DOCKER_CALLS", &calls)
        .output()
        .unwrap();
    let result = result(&output);
    assert_eq!(result["status"], "FAILED");
    assert_eq!(result["failure_reason"], "PROVISIONING_IMAGE_BUILD_FAILED");
    assert!(result.get("derived_task_image_digest").is_none());
    assert!(
        !calls.exists(),
        "unsafe archive must not reach Docker build"
    );
}

fn run_task(manifest: &Path) -> Output {
    Command::new(castor_cli())
        .args(["run", "--task"])
        .arg(manifest)
        .output()
        .expect("run Rust castor CLI")
}

fn result(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "castor run must return one JSON task result: {error}; stdout={}; stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

fn assert_preflight_failure(output: &Output, expected_hash: &str) {
    assert!(!output.status.success(), "invalid snapshot must fail");
    let result = result(output);
    assert_eq!(result["task_id"], "task-snapshot-gate");
    assert_eq!(result["status"], "FAILED");
    assert_eq!(result["failure_reason"], "SNAPSHOT_DIGEST_MISMATCH");
    assert_eq!(result["workspace_snapshot_sha256"], expected_hash);
    assert_eq!(result["committed_turns"], json!([]));
    assert_eq!(result["settled_actions_count"], 0);
    for not_yet_created in [
        "derived_task_image_digest",
        "final_patch_sha256",
        "patch_diff",
        "test_passed",
        "test_exit_code",
    ] {
        assert!(
            result.get(not_yet_created).is_none(),
            "preflight failure must not fabricate {not_yet_created}"
        );
    }
}

#[test]
fn corrupt_snapshot_fails_before_image_build_without_fabricated_result_fields() {
    let root = tempfile::tempdir().unwrap();
    fs::write(root.path().join("snapshot.tar"), b"tampered archive bytes").unwrap();
    let manifest = write_manifest(root.path(), "snapshot.tar");
    assert_preflight_failure(&run_task(&manifest), WRONG_SNAPSHOT_HASH);
}

#[test]
fn snapshot_locator_cannot_traverse_outside_manifest_directory() {
    let root = tempfile::tempdir().unwrap();
    let child = root.path().join("task");
    fs::create_dir(&child).unwrap();
    let real_hash = create_archive(root.path(), "outside.tar");
    let manifest = write_manifest_with_hash(&child, "../outside.tar", &real_hash);
    assert_preflight_failure(&run_task(&manifest), &real_hash);
}

#[test]
fn snapshot_locator_rejects_absolute_path_even_with_valid_archive_hash() {
    let root = tempfile::tempdir().unwrap();
    let real_hash = create_archive(root.path(), "snapshot.tar");
    let absolute = root.path().join("snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), absolute.to_str().unwrap(), &real_hash);
    assert_preflight_failure(&run_task(&manifest), &real_hash);
}

#[test]
fn missing_snapshot_archive_fails_before_image_build() {
    let root = tempfile::tempdir().unwrap();
    let manifest = write_manifest(root.path(), "missing.tar");
    assert_preflight_failure(&run_task(&manifest), WRONG_SNAPSHOT_HASH);
}

#[test]
fn derived_task_image_still_rejects_a_second_sandbox_mount() {
    let root = tempfile::tempdir().unwrap();
    let socket = root.path().join("ipc.sock");
    fs::write(&socket, []).unwrap();
    let mut config =
        build_castor_untrusted_agent_config(BASE_DIGEST.to_owned(), &socket, None, None)
            .expect("single-socket Roche profile");
    config.mounts.push(config.mounts[0].clone());
    let error = RocheSandboxRunner::new(config)
        .start("true")
        .expect_err("extra mount must fail before launching Docker");
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
}

#[test]
fn pi_carrier_workspace_is_physically_read_only() {
    let carrier = "substratum/castor-pi-carrier:v1";
    let inspected = Command::new("docker")
        .args(["image", "inspect", "--format", "{{.Id}}", carrier])
        .output()
        .expect("inspect pinned Pi carrier image");
    assert!(
        inspected.status.success(),
        "T-337-D must build the Python-free Pi carrier image before physical gate: {}",
        String::from_utf8_lossy(&inspected.stderr)
    );
    let image_digest = String::from_utf8_lossy(&inspected.stdout).trim().to_owned();
    assert!(image_digest.starts_with("sha256:"));
    let root = tempfile::tempdir().unwrap();
    let socket = root.path().join("ipc.sock");
    fs::write(&socket, []).unwrap();
    let config = build_castor_untrusted_agent_config(image_digest, &socket, None, None)
        .expect("construct the one-mount Roche profile");
    let supervisor = RocheSandboxRunner::new(config)
        .start("exec sleep 30")
        .expect("launch Pi carrier under real Roche profile");
    let probe = Command::new("docker")
        .args([
            "exec",
            supervisor.container_id(),
            "node",
            "-e",
            "try { require('fs').writeFileSync('/workspace/probe','x'); console.log('WRITABLE'); } catch (error) { console.log(error.code); }",
        ])
        .output()
        .expect("probe container workspace write");
    supervisor.remove().expect("remove physical Pi carrier");
    assert!(probe.status.success());
    assert_eq!(String::from_utf8_lossy(&probe.stdout).trim(), "EROFS");
}

#[test]
fn pi_carrier_contains_pinned_node_and_pi_without_python() {
    let carrier = "substratum/castor-pi-carrier:v1";
    let inspected = Command::new("docker")
        .args(["image", "inspect", "--format", "{{.Id}}", carrier])
        .output()
        .expect("inspect Pi carrier image");
    assert!(
        inspected.status.success(),
        "T-337-D must build the Python-free Pi carrier image: {}",
        String::from_utf8_lossy(&inspected.stderr)
    );
    let image_digest = String::from_utf8_lossy(&inspected.stdout).trim().to_owned();
    let audit = Command::new("docker")
        .args([
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10001:10001",
            &image_digest,
            "sh",
            "-c",
            "node --version; pi --version; if command -v python3 >/dev/null || command -v python >/dev/null; then echo PYTHON_PRESENT; exit 42; fi",
        ])
        .output()
        .expect("audit actual Pi carrier runtime");
    assert!(
        audit.status.success(),
        "Pi carrier must run without Python: stdout={} stderr={}",
        String::from_utf8_lossy(&audit.stdout),
        String::from_utf8_lossy(&audit.stderr)
    );
    let stdout = String::from_utf8_lossy(&audit.stdout);
    let mut lines = stdout.lines();
    let node_version = lines.next().unwrap_or_default();
    let major_minor: Vec<u32> = node_version
        .trim_start_matches('v')
        .split('.')
        .take(2)
        .map(str::parse)
        .collect::<Result<_, _>>()
        .expect("parse Node.js version");
    assert!(major_minor[0] > 22 || (major_minor[0] == 22 && major_minor[1] >= 19));
    assert!(lines.next().unwrap_or_default().contains("0.87.1"));
    assert!(!stdout.contains("PYTHON_PRESENT"));
}

#[cfg(unix)]
#[test]
fn snapshot_locator_rejects_symlinked_archive() {
    use std::os::unix::fs::symlink;

    let root = tempfile::tempdir().unwrap();
    let real_hash = create_archive(root.path(), "real.tar");
    symlink("real.tar", root.path().join("alias.tar")).unwrap();
    let manifest = write_manifest_with_hash(root.path(), "alias.tar", &real_hash);
    assert_preflight_failure(&run_task(&manifest), &real_hash);
}

#[test]
fn agent_crash_before_committed_action_fails_without_effects() {
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let output = run_with_agent_script(root.path(), &manifest, "kill -9 $$", None);
    assert!(!output.status.success());
    let result = result(&output);
    assert_eq!(result["status"], "FAILED");
    assert_eq!(result["failure_reason"], "AGENT_CRASHED");
    assert_eq!(result["committed_turns"], json!([]));
    assert_eq!(result["settled_actions_count"], 0);
    assert!(result.get("final_patch_sha256").is_none());
}

#[test]
fn agent_success_claim_cannot_override_failing_host_verification() {
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let mut body: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    body["verification_command"] = json!(["sh", "-c", "exit 101"]);
    fs::write(&manifest, serde_json::to_vec(&body).unwrap()).unwrap();
    let output = run_with_agent_script(
        root.path(),
        &manifest,
        "printf '%s\\n' \\
  '{\"type\":\"session\",\"version\":3,\"id\":\"00000000-0000-0000-0000-000000000001\",\"timestamp\":\"2026-09-25T00:00:00Z\",\"cwd\":\"/workspace\"}' \\
  '{\"type\":\"agent_start\"}' \\
  '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"All 50 tests passed!\"}],\"stopReason\":\"stop\"}}' \\
  '{\"type\":\"agent_end\",\"messages\":[],\"willRetry\":false}' \\
  '{\"type\":\"agent_settled\"}'",
        None,
    );
    assert!(!output.status.success());
    let result = result(&output);
    assert_eq!(result["status"], "FAILED");
    assert_eq!(result["failure_reason"], "TEST_VERIFICATION_FAILED");
    assert_eq!(result["test_passed"], false);
    assert_eq!(result["test_exit_code"], 101);
}

#[ignore = "spawned only as an untrusted child by the hostile task contract"]
#[test]
fn mock_agent_sends_unauthorized_opcode() {
    let socket = std::env::var("CASTOR_IPC_SOCKET").expect("agent receives only the AISA socket");
    let mut client = GatewayClient::connect(socket).expect("connect to real agent gateway");
    let response = client
        .request(&SyscallRequest {
            request_id: "hostile-grant-1".to_owned(),
            op: "GrantCapability".to_owned(),
            payload: json!({}),
        })
        .expect("gateway responds to unauthorized operation");
    assert_eq!(response.error.unwrap().code, "UnauthorizedOpcode");
}

#[ignore = "spawned only as an untrusted child by the provider-failure contract"]
#[test]
fn mock_agent_requests_model() {
    let socket = std::env::var("CASTOR_IPC_SOCKET").expect("agent receives the AISA socket");
    let agent_id = std::env::var("CASTOR_TEST_AGENT_ID").unwrap_or_else(|_| "agent-1".to_owned());
    let base = std::env::var("CASTOR_TEST_BASE_PROJECTION_DIGEST")
        .unwrap_or_else(|_| EMPTY_REGION_DIGEST.to_owned());
    let mut client = GatewayClient::connect(socket).expect("connect to real agent gateway");
    let admitted = client
        .request(&SyscallRequest {
            request_id: "admit-model-turn".to_owned(),
            op: "AdmitTurn".to_owned(),
            payload: json!({
                "agent_id": agent_id,
                "turn_id": 1,
                "lease_epoch": 0,
                "base_projection_digest": base
            }),
        })
        .expect("admit model turn");
    assert_eq!(admitted.outcome.unwrap()["type"], "Admitted");
    let requested = client
        .request(&SyscallRequest {
            request_id: "request-model".to_owned(),
            op: "RequestInteraction".to_owned(),
            payload: json!({
                "interaction_id": "interaction-model-1",
                "lease_epoch": 0,
                "request_digest": format!("sha256:{:x}", Sha256::digest(b"Repair the failing unit test."))
            }),
        })
        .expect("request governed model interaction");
    assert_eq!(requested.outcome.unwrap()["type"], "InteractionRequested");
    thread::sleep(Duration::from_millis(500));
}

fn mock_agent_call(
    client: &mut GatewayClient,
    request_id: &str,
    op: &str,
    payload: Value,
) -> Value {
    let response = client
        .request(&SyscallRequest {
            request_id: request_id.to_owned(),
            op: op.to_owned(),
            payload,
        })
        .unwrap_or_else(|error| panic!("{op} gateway request: {error}"));
    assert_eq!(response.status, "Ok", "{op}: {response:?}");
    response.outcome.expect("governed outcome")
}

#[ignore = "spawned only as an untrusted child by post-arm crash contracts"]
#[test]
fn mock_agent_commits_workspace_edit() {
    let socket = std::env::var("CASTOR_IPC_SOCKET").expect("agent receives the AISA socket");
    let agent_id = std::env::var("CASTOR_TEST_AGENT_ID").unwrap_or_else(|_| "agent-1".to_owned());
    let base = std::env::var("CASTOR_TEST_BASE_PROJECTION_DIGEST")
        .unwrap_or_else(|_| EMPTY_REGION_DIGEST.to_owned());
    let capability =
        std::env::var("CASTOR_TEST_ACTION_CAP_ID").unwrap_or_else(|_| "capability-1".to_owned());
    let mut client = GatewayClient::connect(socket).expect("connect to real agent gateway");
    let patch =
        b"--- a/defect.txt\n+++ b/defect.txt\n@@ -1 +1 @@\n-failing fixture\n+fixed fixture\n";
    let payload = serde_json::to_vec(&json!({
        "action_type": "WorkspaceEdit",
        "target_path": "defect.txt",
        "patch": String::from_utf8_lossy(patch)
    }))
    .unwrap();
    let payload_digest = format!("sha256:{:x}", Sha256::digest(&payload));
    assert_eq!(
        mock_agent_call(
            &mut client,
            "ensure-action-payload",
            "EnsureRegion",
            json!({
                "region_ref": "region://payload/action-1",
                "content_digest": payload_digest,
                "content": payload,
                "profile": "D1"
            }),
        )["type"],
        "Success"
    );
    assert_eq!(
        mock_agent_call(
            &mut client,
            "admit",
            "AdmitTurn",
            json!({
                "agent_id": agent_id,
                "turn_id": 1,
                "lease_epoch": 0,
                "base_projection_digest": base
            }),
        )["type"],
        "Admitted"
    );
    assert_eq!(
        mock_agent_call(
            &mut client,
            "request-model",
            "RequestInteraction",
            json!({
                "interaction_id": "interaction-1",
                "lease_epoch": 0,
                "request_digest": EMPTY_REGION_DIGEST
            }),
        )["type"],
        "InteractionRequested"
    );
    let unbound = mock_agent_call(
        &mut client,
        "consume-before-model-binding",
        "ConsumeInteraction",
        json!({ "interaction_id": "interaction-1", "lease_epoch": 1 }),
    );
    assert_ne!(unbound["type"], "InteractionConsumed");
    if let Ok(marker) = std::env::var("CASTOR_TEST_FIRST_CONSUME_REJECTED") {
        fs::write(marker, b"rejected").unwrap();
    }
    let deadline = std::time::Instant::now() + Duration::from_secs(3);
    loop {
        let outcome = mock_agent_call(
            &mut client,
            "consume-model-observation",
            "ConsumeInteraction",
            json!({ "interaction_id": "interaction-1", "lease_epoch": 1 }),
        );
        if outcome["type"] == "InteractionConsumed" {
            assert_eq!(outcome["payload"]["interaction_id"], "interaction-1");
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "trusted model service must bind before agent can consume"
        );
        thread::sleep(Duration::from_millis(10));
    }
    let manifest_content = b"action-1\n";
    let manifest_digest = format!("sha256:{:x}", Sha256::digest(manifest_content));
    assert_eq!(
        mock_agent_call(
            &mut client,
            "ensure-action-manifest",
            "EnsureRegion",
            json!({
                "region_ref": "region://manifest",
                "content_digest": manifest_digest,
                "content": manifest_content,
                "profile": "D1"
            }),
        )["type"],
        "Success"
    );
    assert_eq!(
        mock_agent_call(
            &mut client,
            "commit",
            "CommitTurn",
            json!({
                "lease_epoch": 1,
                "base_projection_digest": base,
                "successor_region_id": "region://observation",
                "successor_digest": EMPTY_REGION_DIGEST,
                "action_manifest_region_id": "region://manifest",
                "action_manifest_digest": manifest_digest,
                "action_manifest": ["action-1"],
                "action_bindings": [{
                    "action_id": "action-1",
                    "payload_region_ref": "region://payload/action-1",
                    "payload_digest": payload_digest,
                    "actuator_id": "c04:generic"
                }]
            }),
        )["type"],
        "TurnCommitted"
    );
    assert_eq!(
        mock_agent_call(
            &mut client,
            "register",
            "RegisterAction",
            json!({
                "action_id": "action-1",
                "stable_operation_id": "edit-1",
                "action_family": "c04:generic",
                "target_scope": "workspace:defect.txt"
            }),
        )["type"],
        "ActionRegistered"
    );
    // The fault hook may terminate castord after fsync but before the reply.
    let _ = client.request(&SyscallRequest {
        request_id: "arm".to_owned(),
        op: "PresentAdmissionCertificate".to_owned(),
        payload: json!({
            "action_id": "action-1",
            "target_scope": "workspace:defect.txt",
            "capability_id": capability,
            "generation": 1
        }),
    });
}

#[ignore = "spawned only as an untrusted child by the stale-lease contract"]
#[test]
fn mock_agent_replays_stale_commit() {
    let socket = std::env::var("CASTOR_IPC_SOCKET").expect("agent receives the AISA socket");
    let agent_id = std::env::var("CASTOR_TEST_AGENT_ID").unwrap_or_else(|_| "agent-1".to_owned());
    let base = std::env::var("CASTOR_TEST_BASE_PROJECTION_DIGEST")
        .unwrap_or_else(|_| EMPTY_REGION_DIGEST.to_owned());
    let fence_ready = std::env::var("CASTOR_TEST_FENCE_READY").expect("fence marker path");
    let rejected_marker =
        std::env::var("CASTOR_TEST_STALE_REJECTED").expect("rejection marker path");
    let mut client = GatewayClient::connect(socket).expect("connect to real agent gateway");
    assert_eq!(
        mock_agent_call(
            &mut client,
            "admit-stale-turn",
            "AdmitTurn",
            json!({
                "agent_id": agent_id,
                "turn_id": 1,
                "lease_epoch": 0,
                "base_projection_digest": base
            }),
        )["type"],
        "Admitted"
    );
    let deadline = std::time::Instant::now() + Duration::from_secs(3);
    while !Path::new(&fence_ready).exists() {
        assert!(
            std::time::Instant::now() < deadline,
            "supervisor must persist fence before delayed commit"
        );
        thread::sleep(Duration::from_millis(10));
    }
    let outcome = mock_agent_call(
        &mut client,
        "late-commit",
        "CommitTurn",
        json!({
            "lease_epoch": 0,
            "base_projection_digest": base,
            "successor_region_id": "region://observation",
            "successor_digest": EMPTY_REGION_DIGEST,
            "action_manifest_region_id": "region://manifest",
            "action_manifest_digest": EMPTY_REGION_DIGEST,
            "action_manifest": [],
            "action_bindings": []
        }),
    );
    assert_eq!(outcome["type"], "RejectedStaleAuthority");
    fs::write(rejected_marker, b"rejected").unwrap();
}

#[test]
fn delayed_agent_commit_after_fence_is_rejected_without_dispatch() {
    use std::os::unix::fs::PermissionsExt;

    let _ = castor_cli();
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let child = root.path().join("stale-agent.sh");
    fs::write(
        &child,
        "#!/bin/sh\nexec \"$CASTOR_TEST_MOCK_CHILD_BINARY\" --ignored --exact mock_agent_replays_stale_commit --nocapture\n",
    )
    .unwrap();
    fs::set_permissions(&child, fs::Permissions::from_mode(0o755)).unwrap();
    let fence_ready = root.path().join("fence-ready");
    let rejected = root.path().join("stale-rejected");
    let output = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_FAULT_POINT", "fence_after_admit_before_commit")
        .env("CASTOR_TEST_FENCE_READY", &fence_ready)
        .env("CASTOR_TEST_STALE_REJECTED", &rejected)
        .output()
        .expect("run fenced task with delayed agent");
    let result = result(&output);
    assert_eq!(result["status"], "FENCED_CANCELLED");
    assert_eq!(result["settled_actions_count"], 0);
    assert_eq!(fs::read_to_string(&rejected).unwrap(), "rejected");
    let journal = fs::read_to_string(root.path().join("task-state/core-journal.log"))
        .expect("read real C-01 journal");
    assert!(!journal.contains("TurnCommitted"));
    assert!(!journal.contains("AttemptArmed"));
}

#[test]
fn model_transport_failure_fails_task_without_settled_actions() {
    // Resolve the CLI first so the RED failure is the missing product entry,
    // rather than the local sandbox's optional Unix-socket permission.
    let _ = castor_cli();
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let model_socket = root.path().join("model.sock");
    let model = MockModelService::start(&model_socket, None, None);
    let output = run_with_agent_script(
        root.path(),
        &manifest,
        "exec \"$CASTOR_TEST_MOCK_CHILD_BINARY\" --ignored --exact mock_agent_requests_model --nocapture",
        Some(&model_socket),
    );
    assert!(!output.status.success());
    let result = result(&output);
    assert_eq!(result["status"], "FAILED");
    assert_eq!(result["failure_reason"], "MODEL_INTERACTION_ERROR");
    assert_eq!(result["settled_actions_count"], 0);
    assert!(model.attempts.load(Ordering::SeqCst) > 0);
}

#[test]
fn normal_task_requires_bound_model_settled_edit_and_independent_test() {
    use std::os::unix::fs::PermissionsExt;

    let _ = castor_cli();
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let verification_marker = root.path().join("normal-host-test-ran.txt");
    let mut body: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    body["verification_command"] = json!([
        "sh",
        "-c",
        format!(
            "test \"$(cat defect.txt)\" = \"fixed fixture\" && printf verified > '{}'",
            verification_marker.display()
        )
    ]);
    fs::write(&manifest, serde_json::to_vec(&body).unwrap()).unwrap();
    let child = root.path().join("editing-agent.sh");
    fs::write(
        &child,
        "#!/bin/sh\nexec \"$CASTOR_TEST_MOCK_CHILD_BINARY\" --ignored --exact mock_agent_commits_workspace_edit --nocapture\n",
    )
    .unwrap();
    fs::set_permissions(&child, fs::Permissions::from_mode(0o755)).unwrap();
    let model_socket = root.path().join("model.sock");
    let first_rejection = root.path().join("first-unbound-rejection");
    let model = MockModelService::start(
        &model_socket,
        Some(buffered_model_response()),
        Some(first_rejection.clone()),
    );
    let output = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_MODEL_SOCKET", &model_socket)
        .env("CASTOR_TEST_FIRST_CONSUME_REJECTED", &first_rejection)
        .env("CASTOR_TEST_ACTUATOR_MODE", "apply_and_settle")
        .output()
        .expect("run governed normal task");
    assert!(
        output.status.success(),
        "task stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result = result(&output);
    assert_eq!(result["status"], "SUCCEEDED");
    assert_eq!(result["failure_reason"], "NONE");
    assert_eq!(result["derived_task_image_digest"], BASE_DIGEST);
    assert_eq!(result["test_passed"], true);
    assert_eq!(result["test_exit_code"], 0);
    assert_eq!(result["committed_turns"], json!([1]));
    assert_eq!(result["settled_actions_count"], 1);
    assert!(result["patch_diff"]
        .as_str()
        .unwrap()
        .contains("+fixed fixture"));
    assert_eq!(
        fs::read_to_string(&verification_marker).unwrap(),
        "verified"
    );
    assert_eq!(fs::read_to_string(&first_rejection).unwrap(), "rejected");
    assert!(model.attempts.load(Ordering::SeqCst) > 0);
    let journal = fs::read_to_string(root.path().join("task-state/core-journal.log"))
        .expect("read real C-01 journal");
    let positions: Vec<_> = [
        "InteractionRequested",
        "InteractionBound",
        "TurnCommitted",
        "AttemptArmed",
        "AttemptSettled",
    ]
    .iter()
    .map(|entry| {
        journal
            .find(entry)
            .unwrap_or_else(|| panic!("missing {entry} in journal"))
    })
    .collect();
    assert!(positions.windows(2).all(|pair| pair[0] < pair[1]));
}

#[test]
fn post_arm_daemon_crash_recovers_as_unknown_without_retry() {
    use std::os::unix::fs::PermissionsExt;

    let _ = castor_cli();
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let starts = root.path().join("agent-starts.txt");
    let child = root.path().join("arming-agent.sh");
    fs::write(
        &child,
        "#!/bin/sh\nprintf 'started\\n' >> \"$CASTOR_TEST_AGENT_STARTS\"\nexec \"$CASTOR_TEST_MOCK_CHILD_BINARY\" --ignored --exact mock_agent_commits_workspace_edit --nocapture\n",
    )
    .unwrap();
    fs::set_permissions(&child, fs::Permissions::from_mode(0o755)).unwrap();
    let model_socket = root.path().join("model.sock");
    let first_rejection = root.path().join("first-unbound-rejection");
    let model = MockModelService::start(
        &model_socket,
        Some(buffered_model_response()),
        Some(first_rejection.clone()),
    );
    let first = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_AGENT_STARTS", &starts)
        .env("CASTOR_TEST_MODEL_SOCKET", &model_socket)
        .env("CASTOR_TEST_FIRST_CONSUME_REJECTED", &first_rejection)
        .env("CASTOR_TEST_FAULT_POINT", "crash_post_attempt_armed")
        .output()
        .expect("run until post-arm crash");
    assert!(
        !first.status.success(),
        "fault must terminate the first run"
    );

    let recovered = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_AGENT_STARTS", &starts)
        .output()
        .expect("reopen durable task after crash");
    let result = result(&recovered);
    assert_eq!(result["status"], "UNKNOWN_DISPUTED");
    assert!(model.attempts.load(Ordering::SeqCst) > 0);
    assert_eq!(fs::read_to_string(&starts).unwrap().lines().count(), 1);
    let journal = fs::read_to_string(root.path().join("task-state/core-journal.log"))
        .expect("read real C-01 journal");
    assert!(journal.contains("AttemptArmed"));
    assert!(!journal.contains("AttemptSettled"));
    assert!(!journal.contains("EffectSettled"));
}

#[test]
fn actuator_crash_after_write_must_probe_settle_and_run_host_test() {
    use std::os::unix::fs::PermissionsExt;

    let _ = castor_cli();
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let verification_marker = root.path().join("host-verification-ran.txt");
    let mut body: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    body["verification_command"] = json!([
        "sh",
        "-c",
        format!(
            "test \"$(cat defect.txt)\" = \"fixed fixture\" && printf verified > '{}'",
            verification_marker.display()
        )
    ]);
    fs::write(&manifest, serde_json::to_vec(&body).unwrap()).unwrap();
    let starts = root.path().join("agent-starts.txt");
    let child = root.path().join("arming-agent.sh");
    fs::write(
        &child,
        "#!/bin/sh\nprintf 'started\\n' >> \"$CASTOR_TEST_AGENT_STARTS\"\nexec \"$CASTOR_TEST_MOCK_CHILD_BINARY\" --ignored --exact mock_agent_commits_workspace_edit --nocapture\n",
    )
    .unwrap();
    fs::set_permissions(&child, fs::Permissions::from_mode(0o755)).unwrap();
    let model_socket = root.path().join("model.sock");
    let first_rejection = root.path().join("first-unbound-rejection");
    let model = MockModelService::start(
        &model_socket,
        Some(buffered_model_response()),
        Some(first_rejection.clone()),
    );
    let uncertain = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_AGENT_STARTS", &starts)
        .env("CASTOR_TEST_MODEL_SOCKET", &model_socket)
        .env("CASTOR_TEST_FIRST_CONSUME_REJECTED", &first_rejection)
        .env(
            "CASTOR_TEST_ACTUATOR_MODE",
            "apply_then_crash_before_settlement",
        )
        .output()
        .expect("run through actuator write and crash");
    assert_eq!(result(&uncertain)["status"], "UNKNOWN_DISPUTED");
    assert!(model.attempts.load(Ordering::SeqCst) > 0);
    assert!(
        !verification_marker.exists(),
        "test cannot run before settlement"
    );

    let recovered = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_AGENT_STARTS", &starts)
        .env("CASTOR_TEST_ACTUATOR_MODE", "probe_existing")
        .output()
        .expect("recover applied action from physical evidence");
    let result = result(&recovered);
    assert_eq!(result["status"], "SUCCEEDED");
    assert_eq!(result["test_passed"], true);
    assert_eq!(result["test_exit_code"], 0);
    assert_eq!(
        fs::read_to_string(&verification_marker).unwrap(),
        "verified"
    );
    assert_eq!(fs::read_to_string(&starts).unwrap().lines().count(), 1);
    let journal = fs::read_to_string(root.path().join("task-state/core-journal.log"))
        .expect("read real C-01 journal");
    assert!(journal.contains("AttemptArmed"));
    assert!(journal.contains("AttemptSettled"));
}

#[test]
fn hostile_agent_opcode_is_rejected_and_task_cannot_succeed() {
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let output = run_with_agent_script(
        root.path(),
        &manifest,
        "exec \"$CASTOR_TEST_MOCK_CHILD_BINARY\" --ignored --exact mock_agent_sends_unauthorized_opcode --nocapture",
        None,
    );
    assert!(!output.status.success());
    let result = result(&output);
    assert_eq!(result["status"], "FAILED");
    assert_eq!(result["failure_reason"], "SECURITY_VIOLATION");
    assert_eq!(result["settled_actions_count"], 0);
}

#[test]
fn duplicate_active_submission_reuses_task_without_starting_another_agent() {
    use std::os::unix::fs::PermissionsExt;
    use std::thread;
    use std::time::{Duration, Instant};

    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let starts = root.path().join("agent-starts.txt");
    let child = root.path().join("slow-agent.sh");
    fs::write(
        &child,
        "#!/bin/sh\nprintf 'started\\n' >> \"$CASTOR_TEST_AGENT_STARTS\"\nsleep 2\nexit 7\n",
    )
    .unwrap();
    fs::set_permissions(&child, fs::Permissions::from_mode(0o755)).unwrap();

    let mut first = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_AGENT_STARTS", &starts)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start first task");
    let deadline = Instant::now() + Duration::from_secs(3);
    while !starts.exists() {
        if Instant::now() >= deadline || first.try_wait().unwrap().is_some() {
            let _ = first.kill();
            let _ = first.wait();
            panic!("first task did not start the controlled agent");
        }
        thread::sleep(Duration::from_millis(10));
    }
    let second = task_command(root.path(), &manifest, &child)
        .env("CASTOR_TEST_AGENT_STARTS", &starts)
        .output()
        .expect("resubmit active idempotency key");
    let first = first.wait_with_output().expect("collect first task result");
    assert_eq!(result(&second)["task_id"], result(&first)["task_id"]);
    assert_eq!(fs::read_to_string(&starts).unwrap().lines().count(), 1);
}
