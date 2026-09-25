use crate::host::{GatewayClient, SyscallRequest};
use crate::one_shot::image::StagedSnapshot;
use crate::one_shot::manifest::TaskManifest;
use crate::one_shot::model::TestModelService;
use crate::one_shot::result::TaskResult;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::AsRawFd;
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
    result: Option<Value>,
}

struct BoardLock(File);

struct TestDaemon {
    child: Child,
    agent_socket: PathBuf,
    control_socket: PathBuf,
}

impl TestDaemon {
    fn start(root: &Path) -> io::Result<Self> {
        let daemon_binary = std::env::current_exe()?.with_file_name("castord");
        let agent_socket = root.join("ipc.sock");
        let control_socket = root.join("control.sock");
        let mut child = Command::new(daemon_binary)
            .arg("--storage-root")
            .arg(root)
            .arg("--socket")
            .arg(&agent_socket)
            .arg("--control-socket")
            .arg(&control_socket)
            .arg("--allow-test-opcodes")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(3);
        while UnixStream::connect(&agent_socket).is_err()
            || UnixStream::connect(&control_socket).is_err()
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
        })
    }
}

impl Drop for TestDaemon {
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
            if let Some(result) = &existing.result {
                return Ok(RunOutcome::Replayed(result.clone()));
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
                result: None,
            },
        );
        write_board(state_root, &board)?;
    }

    let daemon = TestDaemon::start(state_root)?;
    let model_service = std::env::var_os("CASTOR_TEST_MODEL_SOCKET").map(|socket| {
        TestModelService::start(
            daemon.control_socket.clone(),
            PathBuf::from(socket),
            manifest.task_prompt.clone(),
        )
    });
    let child = Command::new(child_path)
        .env("CASTOR_IPC_SOCKET", &daemon.agent_socket)
        .env("CASTOR_CONTROL_SOCKET", &daemon.control_socket)
        .env("CASTOR_TEST_TASK_PROMPT", &manifest.task_prompt)
        .output()?;
    let model_failed = model_service.is_some_and(TestModelService::finish);
    if model_failed {
        fence_failed_interaction(&daemon.control_socket)?;
    }
    let result = if model_failed {
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
        TaskResult::after_image(
            manifest.task_id.clone(),
            manifest.workspace_snapshot_sha256.clone(),
            image_digest,
            if code == 0 {
                "UNSETTLED_ACTIONS"
            } else {
                "TEST_VERIFICATION_FAILED"
            },
            Some(code),
        )
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
