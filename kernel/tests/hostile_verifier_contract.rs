//! T-363-B: verifier isolation contract. The isolation cases are intentionally
//! RED while the supervisor executes verification through host Command::new.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::net::TcpListener;
use std::os::unix::net::UnixListener;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const BASE_DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const HOST_DEADLINE: Duration = Duration::from_secs(6);
const LOG_CAP: u64 = 1_048_576;

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
            )
            .env("TMPDIR", self.root.path())
            .process_group(0);
        command
    }

    fn run(&self) -> Value {
        result(run_bounded(self.command()))
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

struct SupervisedChild(Option<Child>);

impl SupervisedChild {
    fn spawn(mut command: Command) -> Self {
        Self(Some(
            command
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        ))
    }

    fn child(&mut self) -> &mut Child {
        self.0.as_mut().unwrap()
    }

    fn kill_group(&self) {
        if let Some(child) = &self.0 {
            // process_group(0) makes this trusted supervisor the group leader.
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
        }
    }

    fn output(mut self) -> Output {
        self.kill_group(); // also kills any child that outlived the supervisor
        self.0.take().unwrap().wait_with_output().unwrap()
    }
}

impl Drop for SupervisedChild {
    fn drop(&mut self) {
        self.kill_group();
        if let Some(mut child) = self.0.take() {
            let _ = child.wait();
        }
    }
}

fn run_bounded(command: Command) -> Output {
    let mut child = SupervisedChild::spawn(command);
    let deadline = Instant::now() + HOST_DEADLINE;
    loop {
        match child.child().try_wait() {
            Ok(Some(_)) => return child.output(),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            _ => {
                panic!("Castor supervisor exceeded the six-second host test deadline");
            }
        }
    }
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
    assert_verifier_accepted_isolation_probe(&result(run_bounded(command)));
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
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let fixture = Fixture::new(&format!(
        "import socket, sys; s = socket.socket(); s.settimeout(1);\ntry: s.connect(('127.0.0.1', {port})); sys.exit(42)\nexcept OSError: sys.exit(0)"
    ));
    assert_verifier_accepted_isolation_probe(&fixture.run());
}

#[test]
fn test_hostile_verifier_timeout_kills_and_fails_closed() {
    let fixture = Fixture::new("import time; time.sleep(12)");
    let started = Instant::now();
    let mut child = SupervisedChild::spawn(fixture.command());
    let deadline = started + HOST_DEADLINE;
    while child.child().try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    if child.child().try_wait().unwrap().is_none() {
        panic!("verifier exceeded the host-side six-second test deadline without being reaped");
    }
    let task = result(child.output());
    assert!(started.elapsed() < HOST_DEADLINE);
    assert_eq!(task["status"], "FAILED");
    assert!(
        task["failure_reason"] == "VerifierTimeout"
            || task["failure_reason"] == "TEST_VERIFICATION_FAILED",
        "unexpected timeout result: {task}"
    );
    assert_ne!(task["test_exit_code"], 0);
}

#[test]
fn test_hostile_verifier_candidate_remains_immutable() {
    let fixture = Fixture::new(
        "import pathlib, sys; p = pathlib.Path('/candidate/defect.txt');\ntry: p.write_text('hostile overwrite'); sys.exit(42)\nexcept OSError as e: sys.exit(0 if e.errno == 30 else 43)",
    );
    let task = fixture.run();
    assert_verifier_accepted_isolation_probe(&task);
}

#[test]
fn test_host_staged_candidate_bytes_remain_unchanged() {
    // The verifier may write /workspace. The actual staged source on the host
    // must remain unchanged while that scratch write is made.
    let fixture = Fixture::new(
        "import pathlib,time; time.sleep(.5); pathlib.Path('defect.txt').write_text('hostile overwrite'); time.sleep(.5)",
    );
    let mut child = SupervisedChild::spawn(fixture.command());
    let deadline = Instant::now() + HOST_DEADLINE;
    let mut staged_before = None;
    let mut staged_after = None;
    while Instant::now() < deadline {
        for entry in fs::read_dir(fixture.root.path()).unwrap().flatten() {
            if !entry
                .file_name()
                .to_string_lossy()
                .starts_with("castor-task-image-")
            {
                continue;
            }
            let path = entry.path().join("workspace_snapshot/defect.txt");
            if path.exists() {
                let hash = digest(&path);
                if staged_before.is_none() {
                    staged_before = Some(hash.clone());
                }
                staged_after = Some(hash);
            }
        }
        if child.child().try_wait().unwrap().is_some() {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    if child.child().try_wait().unwrap().is_none() {
        panic!("staged candidate observation exceeded host deadline");
    }
    let task = result(child.output());
    assert_eq!(
        staged_before,
        Some(digest(&fixture.root.path().join("source/defect.txt"))),
        "actual staged candidate was not observed before verification"
    );
    assert_eq!(
        staged_after, staged_before,
        "host staged candidate bytes changed during verification: {task}"
    );
}

#[test]
fn test_verifier_enforces_resource_limits() {
    // This is observation-only: no fork, allocation stress, or OOM workload.
    // The product must expose independently inspected host evidence before any
    // resource-consuming conformance probe is permitted.
    let task = Fixture::new("pass").run();
    let profile = &task["verifier_evidence"]["inspected_profile"];
    assert_eq!(
        profile["pids_limit"], 256,
        "missing inspected PID cap: {task}"
    );
    let memory = profile["memory_bytes"]
        .as_u64()
        .expect("missing inspected memory cap");
    let memory_swap = profile["memory_swap_bytes"]
        .as_u64()
        .expect("missing explicit memory-plus-swap cap");
    let nano_cpus = profile["nano_cpus"]
        .as_u64()
        .expect("missing inspected CPU cap");
    assert!((1_073_741_824..=2_147_483_648).contains(&memory));
    assert!(memory_swap >= memory && memory_swap <= 2_147_483_648);
    assert!((2_000_000_000..=4_000_000_000).contains(&nano_cpus));
    assert_eq!(task["verifier_evidence"]["cgroup_limits_confirmed"], true);
}

#[test]
fn test_verifier_enforces_log_stream_cap() {
    // 2 MiB per stream is finite and harmless on the host RED baseline.
    let task = Fixture::new(
        "import sys; sys.stdout.write('o' * 2097152); sys.stderr.write('e' * 2097152)",
    )
    .run();
    let logs = &task["verifier_evidence"]["captured_logs"];
    assert_eq!(
        logs["stdout_bytes"].as_u64(),
        Some(LOG_CAP),
        "stdout capture was not capped: {task}"
    );
    assert_eq!(
        logs["stderr_bytes"].as_u64(),
        Some(LOG_CAP),
        "stderr capture was not capped: {task}"
    );
    assert_eq!(logs["stdout_truncated"], true);
    assert_eq!(logs["stderr_truncated"], true);
}

#[test]
fn test_verifier_oom_or_crash_fails_closed() {
    // Controlled abnormal signal, with core files disabled; never stress host memory.
    let task = Fixture::new("import os,resource,signal; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.kill(os.getpid(),signal.SIGKILL)").run();
    assert_eq!(task["status"], "FAILED");
    assert_eq!(
        task["failure_reason"], "VerifierCrashed",
        "abnormal verifier exit must fail closed distinctly: {task}"
    );
    assert_eq!(task["test_passed"], false);
}

#[test]
fn test_verifier_launcher_failure_fails_closed_without_host_fallback() {
    let fixture = Fixture::new("pass");
    let manifest_path = fixture.root.path().join("manifest.json");
    let mut manifest: Value = serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    manifest["verification_command"] = json!(["/definitely-absent-t363-verifier-launcher"]);
    fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let task = fixture.run();
    assert_eq!(task["status"], "FAILED");
    assert_eq!(
        task["failure_reason"], "VerifierUnavailable",
        "launcher failure must fail closed: {task}"
    );
    assert_eq!(task["test_passed"], false);
}

#[test]
fn test_verifier_setup_inspect_teardown_have_one_deadline_and_cleanup() {
    let task = Fixture::new("pass").run();
    let evidence = &task["verifier_evidence"];
    assert_eq!(
        evidence["deadline_ms"], 5_000,
        "missing five-second whole-operation deadline: {task}"
    );
    assert!(evidence["setup_ms"].as_u64().is_some_and(|ms| ms <= 2_000));
    assert!(evidence["inspect_ms"]
        .as_u64()
        .is_some_and(|ms| ms <= 1_000));
    assert!(evidence["teardown_ms"]
        .as_u64()
        .is_some_and(|ms| ms <= 2_000));
    assert_eq!(
        evidence["container_removed"], true,
        "verifier container was not reaped: {task}"
    );
    assert_eq!(
        evidence["all_verification_paths_isolated"], true,
        "verification path bypassed isolation: {task}"
    );
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
