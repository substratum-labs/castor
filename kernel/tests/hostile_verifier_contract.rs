//! T-363-B: verifier isolation contract. The isolation cases are intentionally
//! RED while the supervisor executes verification through host Command::new.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const BASE_DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn castor_cli() -> &'static str {
    env!("CARGO_BIN_EXE_castor")
}

struct Fixture {
    root: tempfile::TempDir,
}

impl Fixture {
    fn new(program: &str) -> Self {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("source");
        fs::create_dir(&source).unwrap();
        fs::write(source.join("defect.txt"), b"candidate bytes\n").unwrap();
        let archive = root.path().join("snapshot.tar");
        let tar = Command::new("tar")
            .args(["-cf"])
            .arg(&archive)
            .args(["-C"])
            .arg(&source)
            .arg(".")
            .output()
            .unwrap();
        assert!(
            tar.status.success(),
            "tar: {}",
            String::from_utf8_lossy(&tar.stderr)
        );
        let hash = format!("{:x}", Sha256::digest(fs::read(&archive).unwrap()));
        let manifest = json!({
            "task_id": "task-hostile-verifier",
            "idempotency_key": "hostile-verifier-001",
            "carrier_base_image": format!("substratum/castor-pi-carrier:v1@{BASE_DIGEST}"),
            "workspace_snapshot_path": "snapshot.tar",
            "workspace_snapshot_sha256": hash,
            "task_prompt": "Exercise verifier isolation.",
            "verification_command": ["python3", "-c", program]
        });
        fs::write(
            root.path().join("manifest.json"),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        fs::write(root.path().join("agent.sh"), b"#!/bin/sh\nexit 0\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(
            root.path().join("agent.sh"),
            fs::Permissions::from_mode(0o755),
        )
        .unwrap();
        Self { root }
    }

    fn command(&self) -> Command {
        let mut command = Command::new(castor_cli());
        command
            .args(["run", "--allow-test-opcodes", "--task"])
            .arg(self.root.path().join("manifest.json"))
            .env("CASTOR_TEST_AGENT_CHILD", self.root.path().join("agent.sh"))
            .env(
                "CASTOR_TEST_MOCK_CHILD_BINARY",
                std::env::current_exe().unwrap(),
            )
            .env("CASTOR_TEST_DERIVED_IMAGE_DIGEST", BASE_DIGEST)
            .env(
                "CASTOR_TEST_TASK_STATE_ROOT",
                self.root.path().join("task-state"),
            );
        command
    }

    fn run(&self) -> Value {
        result(self.command().output().expect("run Castor task"))
    }

    fn source_digest(&self) -> String {
        digest(&self.root.path().join("source/defect.txt"))
    }
}

fn digest(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}

fn result(output: Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "expected task JSON: {error}; stdout={}; stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

fn assert_verifier_accepted_isolation_probe(result: &Value) {
    assert_eq!(
        result["test_exit_code"], 0,
        "verifier failed isolation probe: {result}"
    );
    assert_eq!(
        result["test_passed"], true,
        "verifier failed isolation probe: {result}"
    );
}

#[test]
fn test_hostile_verifier_rejects_host_secret_access() {
    let fixture = Fixture::new(
        "import os, pathlib, sys; secret = os.environ.get('OPENAI_API_KEY') or os.environ.get('TEST_HOST_SECRET_TOKEN'); ssh = pathlib.Path('/root/.ssh'); sys.exit(41 if secret or ssh.exists() else 0)",
    );
    let mut command = fixture.command();
    command
        .env("OPENAI_API_KEY", "host-secret-sentinel")
        .env("TEST_HOST_SECRET_TOKEN", "host-token-sentinel");
    assert_verifier_accepted_isolation_probe(&result(command.output().unwrap()));
}

#[test]
fn test_hostile_verifier_rejects_socket_access() {
    let root = tempfile::tempdir().unwrap();
    let socket_path = root.path().join("host.sock");
    let _listener = UnixListener::bind(&socket_path).unwrap();
    let program = format!(
        "import socket, sys; s = socket.socket(socket.AF_UNIX);\ntry: s.connect({:?}); sys.exit(42)\nexcept OSError: sys.exit(0)",
        socket_path.to_str().unwrap()
    );
    let fixture = Fixture::new(&program);
    assert_verifier_accepted_isolation_probe(&fixture.run());
}

#[test]
fn test_hostile_verifier_rejects_network_access() {
    let fixture = Fixture::new(
        "import socket, sys; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM);\ntry: s.connect(('8.8.8.8', 53)); sys.exit(42)\nexcept OSError as e: sys.exit(0 if e.errno == 101 else 43)",
    );
    assert_verifier_accepted_isolation_probe(&fixture.run());
}

#[test]
fn test_hostile_verifier_timeout_kills_and_fails_closed() {
    let fixture = Fixture::new("import time; time.sleep(12)");
    let started = Instant::now();
    let mut child = fixture
        .command()
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = started + Duration::from_secs(6);
    while child.try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    if child.try_wait().unwrap().is_none() {
        child.kill().unwrap();
        reap(&mut child);
        panic!("verifier exceeded the host-side six-second test deadline without being reaped");
    }
    let task = result(child.wait_with_output().unwrap());
    assert!(started.elapsed() < Duration::from_secs(6));
    assert_eq!(task["status"], "FAILED");
    assert!(
        task["failure_reason"] == "VerifierTimeout"
            || task["failure_reason"] == "TEST_VERIFICATION_FAILED",
        "unexpected timeout result: {task}"
    );
    assert_ne!(task["test_exit_code"], 0);
}

fn reap(child: &mut Child) {
    child.wait().expect("reap timed-out supervisor");
}

#[test]
fn test_hostile_verifier_candidate_remains_immutable() {
    let fixture = Fixture::new(
        "import pathlib, sys; p = pathlib.Path('defect.txt');\ntry: p.write_text('hostile overwrite'); sys.exit(42)\nexcept OSError as e: sys.exit(0 if e.errno == 30 else 43)",
    );
    let before = fixture.source_digest();
    let task = fixture.run();
    let after = fixture.source_digest();
    assert_verifier_accepted_isolation_probe(&task);
    assert_eq!(after, before, "candidate source bytes changed");
}

#[test]
fn test_hostile_verifier_failure_fails_closed() {
    let fixture = Fixture::new("import sys; sys.exit(71)");
    let task = fixture.run();
    assert_eq!(task["status"], "FAILED");
    assert_eq!(task["failure_reason"], "TEST_VERIFICATION_FAILED");
    assert_eq!(task["test_passed"], false);
    assert_eq!(task["test_exit_code"], 71);
}
