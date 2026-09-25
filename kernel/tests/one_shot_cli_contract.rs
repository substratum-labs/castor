//! T-337-C: public one-shot task contract, intentionally RED until the Rust CLI exists.
//!
//! These cases exercise the user-facing `castor run --task` boundary. They do
//! not invoke a model or require Docker: the rejected inputs must fail before
//! either external dependency is reached.

use castor_kernel::host::{GatewayClient, SyscallRequest};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::Path;
use std::process::{Command, Output, Stdio};

const BASE_DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const WRONG_SNAPSHOT_HASH: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

fn castor_cli() -> &'static str {
    option_env!("CARGO_BIN_EXE_castor")
        .expect("the Rust castor CLI binary is missing; T-337-D must provide castor run")
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
fn run_with_agent_script(root: &Path, manifest: &Path, script: &str) -> Output {
    use std::os::unix::fs::PermissionsExt;

    let child = root.join("mock-agent.sh");
    fs::write(&child, format!("#!/bin/sh\n{script}\n")).unwrap();
    fs::set_permissions(&child, fs::Permissions::from_mode(0o755)).unwrap();
    task_command(root, manifest, &child)
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
    let output = run_with_agent_script(root.path(), &manifest, "kill -9 $$");
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
        "printf '%s\\n' '{\"type\":\"agent_settled\",\"summary\":\"All 50 tests passed!\"}'",
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

#[test]
fn hostile_agent_opcode_is_rejected_and_task_cannot_succeed() {
    let root = tempfile::tempdir().unwrap();
    let hash = create_archive(root.path(), "snapshot.tar");
    let manifest = write_manifest_with_hash(root.path(), "snapshot.tar", &hash);
    let output = run_with_agent_script(
        root.path(),
        &manifest,
        "exec \"$CASTOR_TEST_MOCK_CHILD_BINARY\" --ignored --exact mock_agent_sends_unauthorized_opcode --nocapture",
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
