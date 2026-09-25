use crate::c01_storage::{CoreEntry, D1DurableStorage};
use crate::host::{GatewayClient, SyscallRequest};
use crate::one_shot::actuator::{
    apply_patch, evidence_key_hex, key_hex, settle_receipt, settle_workspace_edit,
    settle_workspace_edit_with_key, ACTUATOR_ISSUER,
};
use crate::one_shot::image::StagedSnapshot;
use crate::one_shot::manifest::TaskManifest;
use crate::one_shot::model::SocketModelService;
use crate::one_shot::result::TaskResult;
use crate::sandbox::{
    build_castor_untrusted_agent_config, RocheProcessSupervisor, RocheSandboxRunner,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub enum RunOutcome {
    Active(Value),
    Terminal(TaskResult),
    Replayed(Value),
}

#[derive(Deserialize, Serialize)]
struct PersistedTask {
    task_id: String,
    idempotency_key: String,
    manifest_digest: String,
    status: String,
    #[serde(default)]
    derived_task_image_digest: Option<String>,
    result: Option<Value>,
}

struct BoardLock(File);

struct TestDaemon {
    child: Child,
    agent_socket: PathBuf,
    control_socket: PathBuf,
    evidence_socket: PathBuf,
    actuator_socket: PathBuf,
    security_audit: PathBuf,
}

impl TestDaemon {
    fn start(root: &Path) -> io::Result<Self> {
        let daemon_binary = std::env::current_exe()?.with_file_name("castord");
        let agent_socket = root.join("ipc.sock");
        let control_socket = root.join("control.sock");
        let evidence_socket = root.join("evidence.sock");
        let actuator_socket = root.join("actuator.sock");
        let security_audit = root.join("security.audit");
        File::create(&security_audit)?;
        let peer_uid = fs::metadata(root)?.uid();
        let evidence_config = root.join("evidence-trust.json");
        let actuator_config = root.join("actuator-trust.json");
        fs::write(
            &evidence_config,
            serde_json::to_vec(&json!({
                "issuer": ACTUATOR_ISSUER,
                "peer_uid": peer_uid,
                "adapter_id": "c04:generic",
                "receipt_algorithm": "HMAC-SHA256",
                "key_hex": evidence_key_hex(),
                "canonical_scopes": {"action-1": "workspace:defect.txt"}
            }))?,
        )?;
        fs::write(
            &actuator_config,
            serde_json::to_vec(&json!({
                "peer_uid": peer_uid,
                "actuator_id": "c04:generic"
            }))?,
        )?;
        let mut child = Command::new(daemon_binary)
            .arg("--storage-root")
            .arg(root)
            .arg("--socket")
            .arg(&agent_socket)
            .arg("--control-socket")
            .arg(&control_socket)
            .arg("--evidence-socket")
            .arg(&evidence_socket)
            .arg("--actuator-socket")
            .arg(&actuator_socket)
            .arg("--allow-test-opcodes")
            .env("CASTORD_EVIDENCE_TRUST_CONFIG", &evidence_config)
            .env("CASTORD_ACTUATOR_TRUST_CONFIG", &actuator_config)
            .env("CASTORD_SECURITY_AUDIT_PATH", &security_audit)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(3);
        while UnixStream::connect(&agent_socket).is_err()
            || UnixStream::connect(&control_socket).is_err()
            || UnixStream::connect(&evidence_socket).is_err()
            || UnixStream::connect(&actuator_socket).is_err()
        {
            if child.try_wait()?.is_some() || Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(io::Error::other("castord did not open the task gateway"));
            }
            thread::sleep(Duration::from_millis(10));
        }
        Ok(Self {
            child,
            agent_socket,
            control_socket,
            evidence_socket,
            actuator_socket,
            security_audit,
        })
    }
}

impl Drop for TestDaemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Product daemon owns only host channels; the sole guest socket is passed to Roche.
struct ProductDaemon {
    child: Child,
    _socket_root: tempfile::TempDir,
    agent_socket: PathBuf,
    control_socket: PathBuf,
    evidence_socket: PathBuf,
    actuator_socket: PathBuf,
    security_audit: PathBuf,
    evidence_key: [u8; 32],
}

impl ProductDaemon {
    fn start(task_state: &Path) -> io::Result<Self> {
        fs::create_dir_all(task_state)?;
        fs::set_permissions(task_state, fs::Permissions::from_mode(0o700))?;
        let socket_root = tempfile::Builder::new()
            .prefix("castor-pi-gateway-")
            .tempdir()?;
        let root = socket_root.path();
        let agent_socket = root.join("ipc.sock");
        let control_socket = root.join("control.sock");
        let evidence_socket = root.join("evidence.sock");
        let actuator_socket = root.join("actuator.sock");
        let security_audit = task_state.join("security.audit");
        File::create(&security_audit)?;
        let mut evidence_key = [0_u8; 32];
        File::open("/dev/urandom")?.read_exact(&mut evidence_key)?;
        let peer_uid = fs::metadata(task_state)?.uid();
        let evidence_config = task_state.join("evidence-trust.json");
        let actuator_config = task_state.join("actuator-trust.json");
        fs::write(
            &evidence_config,
            serde_json::to_vec(&json!({
                "issuer": ACTUATOR_ISSUER,
                "peer_uid": peer_uid,
                "adapter_id": "c04:generic",
                "receipt_algorithm": "HMAC-SHA256",
                "key_hex": key_hex(&evidence_key),
                "canonical_scopes": {},
                "allowed_scope_prefixes": ["workspace:"]
            }))?,
        )?;
        fs::set_permissions(&evidence_config, fs::Permissions::from_mode(0o600))?;
        fs::write(
            &actuator_config,
            serde_json::to_vec(&json!({
                "peer_uid": peer_uid,
                "actuator_id": "c04:generic"
            }))?,
        )?;
        fs::set_permissions(&actuator_config, fs::Permissions::from_mode(0o600))?;
        let binary = std::env::current_exe()?.with_file_name("castord");
        let mut child = Command::new(binary)
            .args([
                "--storage-root",
                task_state
                    .to_str()
                    .ok_or_else(|| io::Error::other("non-UTF8 task state path"))?,
            ])
            .arg("--socket")
            .arg(&agent_socket)
            .arg("--control-socket")
            .arg(&control_socket)
            .arg("--evidence-socket")
            .arg(&evidence_socket)
            .arg("--actuator-socket")
            .arg(&actuator_socket)
            .args(["--sandbox", "roche"])
            .env("CASTORD_EVIDENCE_TRUST_CONFIG", &evidence_config)
            .env("CASTORD_ACTUATOR_TRUST_CONFIG", &actuator_config)
            .env("CASTORD_SECURITY_AUDIT_PATH", &security_audit)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(5);
        while UnixStream::connect(&agent_socket).is_err()
            || UnixStream::connect(&control_socket).is_err()
            || UnixStream::connect(&evidence_socket).is_err()
            || UnixStream::connect(&actuator_socket).is_err()
        {
            if child.try_wait()?.is_some() || Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(io::Error::other(
                    "product castord did not open host/guest sockets",
                ));
            }
            thread::sleep(Duration::from_millis(10));
        }
        Ok(Self {
            child,
            _socket_root: socket_root,
            agent_socket,
            control_socket,
            evidence_socket,
            actuator_socket,
            security_audit,
            evidence_key,
        })
    }
}

impl Drop for ProductDaemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl BoardLock {
    fn acquire(root: &Path) -> io::Result<Self> {
        fs::create_dir_all(root)?;
        fs::set_permissions(root, fs::Permissions::from_mode(0o700))?;
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(root.join(".task-lock"))?;
        // The lock covers only board read/write; the agent runs without holding it.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(file))
    }
}

impl Drop for BoardLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

pub fn run_test_task(
    manifest: &TaskManifest,
    manifest_path: &Path,
    staged: &StagedSnapshot,
    image_digest: String,
    state_root: &Path,
    child_path: &Path,
) -> io::Result<RunOutcome> {
    let manifest_digest = format!("sha256:{:x}", Sha256::digest(fs::read(manifest_path)?));
    {
        let _lock = BoardLock::acquire(state_root)?;
        let mut board = read_board(state_root)?;
        if let Some(existing) = board.get(&manifest.idempotency_key) {
            if existing.manifest_digest != manifest_digest || existing.task_id != manifest.task_id {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "task state is already bound to another manifest",
                ));
            }
            if existing.status == "UNKNOWN_DISPUTED"
                && std::env::var("CASTOR_TEST_ACTUATOR_MODE").ok().as_deref()
                    == Some("probe_existing")
            {
                drop(_lock);
                return recover_applied_edit(manifest, staged, image_digest, state_root);
            }
            if let Some(result) = &existing.result {
                return Ok(RunOutcome::Replayed(result.clone()));
            }
            // A dead owner after an armed attempt has no authority to retry
            // the effect. The durable C-01 journal, not the board's ACTIVE
            // marker, decides whether recovery must stop as disputed.
            if let Ok(storage) = D1DurableStorage::open(state_root) {
                let armed = storage
                    .journal_requests()
                    .iter()
                    .any(|request| matches!(request.entry, CoreEntry::AttemptArmed { .. }));
                if armed {
                    let mut result = TaskResult::after_image(
                        manifest.task_id.clone(),
                        manifest.workspace_snapshot_sha256.clone(),
                        image_digest.clone(),
                        "UNSETTLED_ACTIONS",
                        None,
                    );
                    result.status = "UNKNOWN_DISPUTED";
                    return Ok(RunOutcome::Terminal(result));
                }
            }
            return Ok(RunOutcome::Active(json!({
                "task_id": existing.task_id,
                "status": "ACTIVE"
            })));
        }
        if board.values().any(|task| task.task_id == manifest.task_id) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "task ID is already bound to another idempotency key",
            ));
        }
        board.insert(
            manifest.idempotency_key.clone(),
            PersistedTask {
                task_id: manifest.task_id.clone(),
                idempotency_key: manifest.idempotency_key.clone(),
                manifest_digest: manifest_digest.clone(),
                status: "ACTIVE".to_owned(),
                derived_task_image_digest: Some(image_digest.clone()),
                result: None,
            },
        );
        write_board(state_root, &board)?;
    }

    let daemon = TestDaemon::start(state_root)?;
    let model_service = std::env::var_os("CASTOR_TEST_MODEL_SOCKET").map(|socket| {
        SocketModelService::start(
            daemon.control_socket.clone(),
            PathBuf::from(socket),
            Duration::from_secs(2),
        )
    });
    let fault_point = std::env::var("CASTOR_TEST_FAULT_POINT").ok();
    let mut child_command = Command::new(child_path);
    child_command
        .env("CASTOR_IPC_SOCKET", &daemon.agent_socket)
        .env("CASTOR_CONTROL_SOCKET", &daemon.control_socket)
        .env("CASTOR_TEST_TASK_PROMPT", &manifest.task_prompt);
    let child = if fault_point.as_deref() == Some("fence_after_admit_before_commit") {
        let process = child_command
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        let marker = std::env::var_os("CASTOR_TEST_FENCE_READY").map(PathBuf::from);
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut fenced = false;
        while Instant::now() < deadline {
            let journal = inspect_journal(&daemon.control_socket)?;
            if journal
                .iter()
                .any(|entry| entry.get("LeaseGranted").is_some())
            {
                fence_failed_interaction(&daemon.control_socket)?;
                if let Some(marker) = marker {
                    fs::write(marker, b"fenced")?;
                }
                fenced = true;
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        if !fenced {
            return Err(io::Error::other(
                "test child did not admit a turn before fence",
            ));
        }
        process.wait_with_output()?
    } else {
        child_command.output()?
    };
    let model_failed = model_service.is_some_and(SocketModelService::finish);
    let security_violation = fs::metadata(&daemon.security_audit)?.len() > 0;
    if model_failed || security_violation {
        fence_failed_interaction(&daemon.control_socket)?;
    }
    if fault_point.as_deref() == Some("crash_post_attempt_armed")
        && inspect_journal(&daemon.control_socket)?
            .iter()
            .any(|entry| entry.get("AttemptArmed").is_some())
    {
        drop(daemon);
        std::process::exit(86);
    }
    let settled_edit = if !model_failed
        && child.status.success()
        && std::env::var("CASTOR_TEST_ACTUATOR_MODE").ok().as_deref() == Some("apply_and_settle")
    {
        settle_workspace_edit(
            &daemon.control_socket,
            &daemon.actuator_socket,
            &daemon.evidence_socket,
            &staged.workspace(),
            true,
        )?
    } else {
        None
    };
    let uncertain_edit = if !model_failed
        && child.status.success()
        && std::env::var("CASTOR_TEST_ACTUATOR_MODE").ok().as_deref()
            == Some("apply_then_crash_before_settlement")
    {
        let durable_workspace = state_root.join("applied-workspace");
        fs::create_dir_all(&durable_workspace)?;
        fs::copy(
            staged.workspace().join("defect.txt"),
            durable_workspace.join("defect.txt"),
        )?;
        let edit = settle_workspace_edit(
            &daemon.control_socket,
            &daemon.actuator_socket,
            &daemon.evidence_socket,
            &durable_workspace,
            false,
        )?;
        if let Some(edit) = &edit {
            let postimage = fs::read(durable_workspace.join("defect.txt"))?;
            File::open(durable_workspace.join("defect.txt"))?.sync_all()?;
            let record = json!({
                "patch": edit.patch,
                "patch_sha256": edit.patch_sha256,
                "target_path": edit.target_path,
                "postimage_sha256": format!("sha256:{:x}", Sha256::digest(postimage))
            });
            let mut file = File::create(state_root.join("applied-edit.json"))?;
            file.write_all(&serde_json::to_vec(&record).map_err(io::Error::other)?)?;
            file.sync_all()?;
            File::open(state_root)?.sync_all()?;
        }
        edit
    } else {
        None
    };
    let result = if uncertain_edit.is_some() {
        let mut result = TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "UNSETTLED_ACTIONS",
            None,
        );
        result.status = "UNKNOWN_DISPUTED";
        result
    } else if security_violation {
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "SECURITY_VIOLATION",
            None,
        )
    } else if fault_point.as_deref() == Some("fence_after_admit_before_commit") {
        let mut result = TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "STALE_AUTHORITY",
            None,
        );
        result.status = "FENCED_CANCELLED";
        result
    } else if model_failed {
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "MODEL_INTERACTION_ERROR",
            None,
        )
    } else if !child.status.success() {
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "AGENT_CRASHED",
            None,
        )
    } else {
        let mut command = Command::new(&manifest.verification_command[0]);
        let test = command
            .args(&manifest.verification_command[1..])
            .current_dir(staged.workspace())
            .output();
        let code = match test {
            Ok(output) => output.status.code().unwrap_or(1),
            Err(_) => 1,
        };
        let mut result = TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            if code == 0 && settled_edit.is_some() {
                "NONE"
            } else if code == 0 {
                "UNSETTLED_ACTIONS"
            } else {
                "TEST_VERIFICATION_FAILED"
            },
            Some(code),
        );
        if code == 0 {
            if let Some(edit) = settled_edit {
                result.status = "SUCCEEDED";
                result.committed_turns = vec![1];
                result.settled_actions_count = 1;
                result.final_patch_sha256 = Some(edit.patch_sha256);
                result.patch_diff = Some(edit.patch);
            }
        }
        result
    };
    {
        let _lock = BoardLock::acquire(state_root)?;
        let mut board = read_board(state_root)?;
        let task = board.get_mut(&manifest.idempotency_key).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "active task disappeared from board",
            )
        })?;
        if task.manifest_digest != manifest_digest {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "active task manifest changed during execution",
            ));
        }
        task.status = result.status.to_owned();
        task.result = Some(serde_json::to_value(&result).map_err(io::Error::other)?);
        write_board(state_root, &board)?;
    }
    Ok(RunOutcome::Terminal(result))
}

/// Run the pinned Pi carrier with only its AISA guest socket inside Roche.
/// `CASTOR_MODEL_SOCKET` is a host-local, user-selected model provider adapter;
/// provider credentials never enter the carrier environment.
pub fn run_product_task(
    manifest: &TaskManifest,
    manifest_path: &Path,
    staged: &StagedSnapshot,
    image_digest: String,
    state_root: &Path,
    model_socket: &Path,
) -> io::Result<RunOutcome> {
    let manifest_digest = format!("sha256:{:x}", Sha256::digest(fs::read(manifest_path)?));
    let task_state = state_root.join("tasks").join(&manifest.task_id);
    {
        let _lock = BoardLock::acquire(state_root)?;
        let mut board = read_board(state_root)?;
        if let Some(existing) = board.get(&manifest.idempotency_key) {
            if existing.manifest_digest != manifest_digest || existing.task_id != manifest.task_id {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "task key is bound to another manifest",
                ));
            }
            if let Some(result) = &existing.result {
                return Ok(RunOutcome::Replayed(result.clone()));
            }
            if let Ok(storage) = D1DurableStorage::open(&task_state) {
                if storage
                    .journal_requests()
                    .iter()
                    .any(|entry| matches!(entry.entry, CoreEntry::AttemptArmed { .. }))
                {
                    let mut result = TaskResult::after_image(
                        manifest.task_id.clone(),
                        manifest.workspace_snapshot_sha256.clone(),
                        image_digest,
                        "UNSETTLED_ACTIONS",
                        None,
                    );
                    result.status = "UNKNOWN_DISPUTED";
                    return Ok(RunOutcome::Terminal(result));
                }
            }
            return Ok(RunOutcome::Active(
                json!({"task_id": existing.task_id, "status": "ACTIVE"}),
            ));
        }
        if board.values().any(|task| task.task_id == manifest.task_id) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "task ID is bound to another idempotency key",
            ));
        }
        board.insert(
            manifest.idempotency_key.clone(),
            PersistedTask {
                task_id: manifest.task_id.clone(),
                idempotency_key: manifest.idempotency_key.clone(),
                manifest_digest: manifest_digest.clone(),
                status: "ACTIVE".to_owned(),
                derived_task_image_digest: Some(image_digest.clone()),
                result: None,
            },
        );
        write_board(state_root, &board)?;
    }

    if !model_socket.exists() {
        let result = TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "MODEL_INTERACTION_ERROR",
            None,
        );
        persist_product_result(state_root, manifest, &manifest_digest, &result)?;
        return Ok(RunOutcome::Terminal(result));
    }
    let daemon = ProductDaemon::start(&task_state)?;
    let model = SocketModelService::start(
        daemon.control_socket.clone(),
        model_socket.to_path_buf(),
        Duration::from_secs(120),
    );
    let mut config =
        build_castor_untrusted_agent_config(image_digest.clone(), &daemon.agent_socket, None, None)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    config.env.insert(
        "CASTOR_TASK_PROMPT".to_owned(),
        manifest.task_prompt.clone(),
    );
    let command = "cd /workspace && exec pi --extension /opt/castor/castor-pi-extension.js --no-extensions --no-builtin-tools --no-session --offline --no-context-files --no-skills --no-prompt-templates --no-themes --model castor/castor-task --mode json --print \"$CASTOR_TASK_PROMPT\" </dev/null";
    let child_exit = match RocheSandboxRunner::new(config).start(command) {
        Ok(carrier) => {
            let exit = wait_for_pi(&carrier, &model);
            if let Ok(logs) = Command::new("docker")
                .args(["logs", "--tail", "200", carrier.container_id()])
                .output()
            {
                let limit = logs.stdout.len().min(1024 * 1024);
                let _ = fs::write(task_state.join("pi.jsonl"), &logs.stdout[..limit]);
            }
            let _ = carrier.remove();
            exit.unwrap_or(Some(1))
        }
        Err(_) => Some(1),
    };
    let model_failed = model.finish();
    let security_violation = fs::metadata(&daemon.security_audit)?.len() > 0;
    if model_failed || security_violation {
        fence_failed_interaction(&daemon.control_socket)?;
    }
    let armed = inspect_journal(&daemon.control_socket)?
        .iter()
        .any(|entry| entry.get("AttemptArmed").is_some());
    let mut result = if security_violation {
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "SECURITY_VIOLATION",
            None,
        )
    } else if model_failed {
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "MODEL_INTERACTION_ERROR",
            None,
        )
    } else if child_exit.is_none() {
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "TIMEOUT_EXCEEDED",
            None,
        )
    } else if child_exit != Some(0) {
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            "AGENT_CRASHED",
            None,
        )
    } else {
        match settle_workspace_edit_with_key(
            &daemon.control_socket,
            &daemon.actuator_socket,
            &daemon.evidence_socket,
            &staged.workspace(),
            true,
            &daemon.evidence_key,
        ) {
            Ok(Some(edit)) => {
                let code = Command::new(&manifest.verification_command[0])
                    .args(&manifest.verification_command[1..])
                    .current_dir(staged.workspace())
                    .output()
                    .map(|output| output.status.code().unwrap_or(1))
                    .unwrap_or(1);
                let mut result = TaskResult::after_image(
                    manifest.task_id.clone(),
                    manifest.workspace_snapshot_sha256.clone(),
                    image_digest,
                    if code == 0 {
                        "NONE"
                    } else {
                        "TEST_VERIFICATION_FAILED"
                    },
                    Some(code),
                );
                result.final_patch_sha256 = Some(edit.patch_sha256);
                result.patch_diff = Some(edit.patch);
                if code == 0 {
                    result.status = "SUCCEEDED";
                }
                result
            }
            Ok(None) => TaskResult::after_image(
                manifest.task_id.clone(),
                manifest.workspace_snapshot_sha256.clone(),
                image_digest,
                "UNSETTLED_ACTIONS",
                None,
            ),
            Err(_) => {
                let mut result = TaskResult::after_image(
                    manifest.task_id.clone(),
                    manifest.workspace_snapshot_sha256.clone(),
                    image_digest,
                    "UNSETTLED_ACTIONS",
                    None,
                );
                if armed {
                    result.status = "UNKNOWN_DISPUTED";
                }
                result
            }
        }
    };
    let journal = inspect_journal(&daemon.control_socket)?;
    result.committed_turns = journal
        .iter()
        .filter_map(|entry| entry.get("TurnCommitted")?.get("turn_id")?.as_u64())
        .collect();
    result.settled_actions_count = journal
        .iter()
        .filter(|entry| entry.get("AttemptSettled").is_some())
        .count() as u64;
    if result.status == "SUCCEEDED"
        && (result.committed_turns.is_empty() || result.settled_actions_count != 1)
    {
        result.status = "UNKNOWN_DISPUTED";
        result.failure_reason = "UNSETTLED_ACTIONS";
    }
    persist_product_result(state_root, manifest, &manifest_digest, &result)?;
    Ok(RunOutcome::Terminal(result))
}

fn wait_for_pi(
    carrier: &RocheProcessSupervisor,
    model: &SocketModelService,
) -> io::Result<Option<i32>> {
    let deadline = Instant::now() + Duration::from_secs(300);
    loop {
        if model.has_failed() {
            let _ = carrier.kill_immediate();
            return Ok(Some(1));
        }
        let output = Command::new("docker")
            .args([
                "inspect",
                "--format",
                "{{.State.Running}} {{.State.ExitCode}}",
                carrier.container_id(),
            ])
            .output()?;
        if !output.status.success() {
            return Err(io::Error::other("Roche container inspection failed"));
        }
        let state = String::from_utf8_lossy(&output.stdout);
        let mut fields = state.split_whitespace();
        if fields.next() == Some("false") {
            return fields
                .next()
                .and_then(|value| value.parse().ok())
                .map(Some)
                .ok_or_else(|| io::Error::other("missing Roche exit code"));
        }
        if Instant::now() >= deadline {
            let _ = carrier.kill_immediate();
            return Ok(None);
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn persist_product_result(
    state_root: &Path,
    manifest: &TaskManifest,
    manifest_digest: &str,
    result: &TaskResult,
) -> io::Result<()> {
    let _lock = BoardLock::acquire(state_root)?;
    let mut board = read_board(state_root)?;
    let task = board
        .get_mut(&manifest.idempotency_key)
        .ok_or_else(|| io::Error::other("task disappeared from board"))?;
    if task.manifest_digest != manifest_digest {
        return Err(io::Error::other("task manifest changed during execution"));
    }
    task.status = result.status.to_owned();
    task.result = Some(serde_json::to_value(result).map_err(io::Error::other)?);
    write_board(state_root, &board)
}

fn recover_applied_edit(
    manifest: &TaskManifest,
    staged: &StagedSnapshot,
    image_digest: String,
    state_root: &Path,
) -> io::Result<RunOutcome> {
    let record: Value = serde_json::from_slice(&fs::read(state_root.join("applied-edit.json"))?)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let patch = record["patch"]
        .as_str()
        .ok_or_else(|| io::Error::other("missing applied patch"))?;
    let target_path = record["target_path"]
        .as_str()
        .ok_or_else(|| io::Error::other("missing applied edit target"))?;
    let patch_digest = format!("sha256:{:x}", Sha256::digest(patch.as_bytes()));
    if record["patch_sha256"] != patch_digest {
        return Err(io::Error::other("applied patch digest mismatch"));
    }
    apply_patch(&staged.workspace(), target_path, patch)?;
    let expected_postimage = format!(
        "sha256:{:x}",
        Sha256::digest(fs::read(staged.workspace().join(target_path))?)
    );
    let observed_postimage = format!(
        "sha256:{:x}",
        Sha256::digest(fs::read(
            state_root.join("applied-workspace").join(target_path)
        )?)
    );
    if record["postimage_sha256"] != observed_postimage || expected_postimage != observed_postimage
    {
        return Err(io::Error::other(
            "physical actuator probe disagrees with committed edit",
        ));
    }
    let daemon = TestDaemon::start(state_root)?;
    let journal = inspect_journal(&daemon.control_socket)?;
    if !journal
        .iter()
        .any(|entry| entry.get("AttemptArmed").is_some())
        || !journal
            .iter()
            .any(|entry| entry.get("DispatchAttempt").is_some())
        || !journal
            .iter()
            .any(|entry| entry.get("AdapterSubmissionRecorded").is_some())
        || journal
            .iter()
            .any(|entry| entry.get("AttemptSettled").is_some())
    {
        return Err(io::Error::other(
            "committed attempt is not eligible for probe settlement",
        ));
    }
    settle_receipt(
        &daemon.control_socket,
        &daemon.evidence_socket,
        1,
        "workspace:defect.txt",
    )?;
    let test = Command::new(&manifest.verification_command[0])
        .args(&manifest.verification_command[1..])
        .current_dir(staged.workspace())
        .output()?;
    let code = test.status.code().unwrap_or(1);
    let mut result = TaskResult::after_image(
        manifest.task_id.clone(),
        manifest.workspace_snapshot_sha256.clone(),
        image_digest,
        if code == 0 {
            "NONE"
        } else {
            "TEST_VERIFICATION_FAILED"
        },
        Some(code),
    );
    result.committed_turns = vec![1];
    result.settled_actions_count = 1;
    if code == 0 {
        result.status = "SUCCEEDED";
        result.patch_diff = Some(patch.to_owned());
        result.final_patch_sha256 = Some(patch_digest);
    }
    let _lock = BoardLock::acquire(state_root)?;
    let mut board = read_board(state_root)?;
    let task = board
        .get_mut(&manifest.idempotency_key)
        .ok_or_else(|| io::Error::other("recovery task missing from board"))?;
    task.status = result.status.to_owned();
    task.result = Some(serde_json::to_value(&result).map_err(io::Error::other)?);
    write_board(state_root, &board)?;
    Ok(RunOutcome::Terminal(result))
}

fn inspect_journal(control_socket: &Path) -> io::Result<Vec<Value>> {
    let mut control = GatewayClient::connect(control_socket)?;
    let response = control.request(&SyscallRequest {
        request_id: "supervisor-inspect-journal".to_owned(),
        op: "InspectJournal".to_owned(),
        payload: json!({}),
    })?;
    response
        .outcome
        .and_then(|value| value.get("entries").and_then(Value::as_array).cloned())
        .ok_or_else(|| io::Error::other("missing trusted journal inspection"))
}

fn fence_failed_interaction(control_socket: &Path) -> io::Result<()> {
    let mut control = GatewayClient::connect(control_socket)?;
    let summary = control.request(&SyscallRequest {
        request_id: "model-failure-summary".to_owned(),
        op: "GetProjectionSummary".to_owned(),
        payload: json!({}),
    })?;
    let generation = summary
        .outcome
        .as_ref()
        .and_then(|value| value.get("generation"))
        .and_then(Value::as_u64)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "missing core generation"))?;
    let next = generation
        .checked_add(1)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "core generation overflow"))?;
    let fenced = control.request(&SyscallRequest {
        request_id: "model-failure-fence".to_owned(),
        op: "PersistFence".to_owned(),
        payload: json!({ "generation": next }),
    })?;
    if fenced.outcome.as_ref().and_then(|value| value.get("type"))
        != Some(&json!("GenerationFenced"))
    {
        return Err(io::Error::other("failed to persist provider-failure fence"));
    }
    Ok(())
}

fn read_board(root: &Path) -> io::Result<HashMap<String, PersistedTask>> {
    let path = root.join("board.json");
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(HashMap::new()),
        Err(error) => Err(error),
    }
}

fn write_board(root: &Path, board: &HashMap<String, PersistedTask>) -> io::Result<()> {
    let path = root.join("board.json");
    let temporary = root.join(format!(".board-{}.tmp", std::process::id()));
    let bytes = serde_json::to_vec(board).map_err(io::Error::other)?;
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    fs::rename(&temporary, &path)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

pub fn test_state_root() -> io::Result<PathBuf> {
    std::env::var_os("CASTOR_TEST_TASK_STATE_ROOT")
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing test task state root"))
}
