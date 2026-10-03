//! Source-checkout, local-model developer launcher. All Docker mutations are
//! scoped to full recorded CIDs and an exclusively allocated private scratch.
use castor_kernel::one_shot::developer_budget::{forward_to_local_adapter, BudgetLedger};
use castor_kernel::one_shot::install::{HostScript, InstalledRelease};
use castor_kernel::one_shot::project::engine::{pack_project, pack_project_with_carrier};
use castor_kernel::one_shot::runtime_prepare::{check_node_major, revalidate, DockerEngine};
use serde_json::{json, Value};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MAX_COMMAND_OUTPUT: u64 = 2 * 1024 * 1024;
const WORKLOAD: Duration = Duration::from_secs(300);
const CLEANUP: Duration = Duration::from_secs(60);
const CONTROLLER_TAG: &str = "substratum/castor-controller:developer-local";

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn random_token() -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    if unsafe { libc::getentropy(bytes.as_mut_ptr().cast(), bytes.len()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn full_cid(value: &str) -> io::Result<&str> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err(invalid("Docker identity is not a full lowercase CID"));
    }
    Ok(value)
}

fn bounded_command(mut command: Command, timeout: Duration) -> io::Result<std::process::Output> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| invalid("missing command stdout"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| invalid("missing command stderr"))?;
    let out_thread = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(MAX_COMMAND_OUTPUT + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let err_thread = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .take(MAX_COMMAND_OUTPUT + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "bounded command timed out",
            ));
        }
        thread::sleep(Duration::from_millis(20));
    };
    let stdout = out_thread
        .join()
        .map_err(|_| invalid("stdout reader panicked"))??;
    let stderr = err_thread
        .join()
        .map_err(|_| invalid("stderr reader panicked"))??;
    if stdout.len() as u64 > MAX_COMMAND_OUTPUT || stderr.len() as u64 > MAX_COMMAND_OUTPUT {
        return Err(invalid("command output exceeds cap"));
    }
    Ok(std::process::Output {
        status,
        stdout,
        stderr,
    })
}

fn write_json(path: &Path, value: &Value) -> io::Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer(&mut file, value).map_err(io::Error::other)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    File::open(
        path.parent()
            .ok_or_else(|| invalid("missing evidence parent"))?,
    )?
    .sync_all()
}

fn path_string(path: &Path) -> io::Result<String> {
    path.to_str()
        .map(str::to_owned)
        .ok_or_else(|| invalid("non-UTF8 mount path"))
}

#[derive(Default)]
struct Docker {
    deadline: Option<Instant>,
}
impl Docker {
    fn call(
        &self,
        args: &[String],
        timeout: Duration,
        check: bool,
    ) -> io::Result<std::process::Output> {
        let mut command = Command::new("docker");
        command.args(args);
        let timeout = if let Some(deadline) = self.deadline {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "Docker lifecycle deadline exhausted",
                ));
            }
            timeout.min(remaining)
        } else {
            timeout
        };
        let output = bounded_command(command, timeout)?;
        if check && !output.status.success() {
            return Err(io::Error::other(format!(
                "docker {} failed: {}",
                args.first().map_or("?", String::as_str),
                String::from_utf8_lossy(&output.stderr)
            )));
        }
        Ok(output)
    }

    fn text(&self, args: &[String], timeout: Duration) -> io::Result<String> {
        let output = self.call(args, timeout, true)?;
        String::from_utf8(output.stdout)
            .map(|s| s.trim().to_owned())
            .map_err(|_| invalid("non-UTF8 Docker output"))
    }

    fn inspect(&self, cid: &str) -> io::Result<Option<Value>> {
        let output = self.call(
            &["inspect".into(), full_cid(cid)?.into()],
            Duration::from_secs(15),
            false,
        )?;
        if !output.status.success() {
            let error = String::from_utf8_lossy(&output.stderr).to_lowercase();
            if error.contains("no such object") || error.contains("no such container") {
                self.text(
                    &[
                        "info".into(),
                        "--format".into(),
                        "{{.DockerRootDir}}".into(),
                    ],
                    Duration::from_secs(15),
                )?;
                return Ok(None);
            }
            return Err(io::Error::other(format!("docker inspect failed: {error}")));
        }
        let mut items: Vec<Value> =
            serde_json::from_slice(&output.stdout).map_err(io::Error::other)?;
        let item = items.pop().ok_or_else(|| invalid("empty Docker inspect"))?;
        if !items.is_empty() || item.get("Id").and_then(Value::as_str) != Some(cid) {
            return Err(invalid("Docker inspect identity mismatch"));
        }
        Ok(Some(item))
    }

    fn inventory(&self) -> io::Result<Vec<Value>> {
        let ids = self.text(
            &["ps".into(), "-aq".into(), "--no-trunc".into()],
            Duration::from_secs(15),
        )?;
        let ids: Vec<_> = ids.split_whitespace().collect();
        if ids.len() > 256 {
            return Err(invalid("Docker inventory exceeds bounded audit"));
        }
        let mut items = Vec::new();
        for id in ids {
            if let Some(item) = self.inspect(full_cid(id)?)? {
                items.push(item);
            }
        }
        Ok(items)
    }
}

fn mount_source<'a>(item: &'a Value, destination: &str) -> io::Result<&'a str> {
    item.get("Mounts")
        .and_then(Value::as_array)
        .and_then(|mounts| {
            mounts
                .iter()
                .find(|m| m.get("Destination").and_then(Value::as_str) == Some(destination))
        })
        .and_then(|m| m.get("Source").and_then(Value::as_str))
        .ok_or_else(|| invalid("required Docker bind absent"))
}

fn inside(path: &str, root: &str) -> bool {
    Path::new(path).is_absolute()
        && !Path::new(path)
            .components()
            .any(|part| matches!(part, std::path::Component::ParentDir))
        && Path::new(path).starts_with(root)
        && path != root
}

fn overlaps(left: &str, right: &str) -> bool {
    left == right || inside(left, right) || inside(right, left)
}

fn observed_child_profile(item: &Value, scratch: &str, verifier_id: &str) -> Option<&'static str> {
    let mounts = item.get("Mounts")?.as_array()?;
    if mounts.len() != 1 {
        return None;
    }
    let mount = &mounts[0];
    let source = mount.get("Source")?.as_str()?;
    let destination = mount.get("Destination")?.as_str()?;
    if !inside(source, scratch)
        || mount.get("Type")?.as_str()? != "bind"
        || mount.get("RW")?.as_bool()?
    {
        return None;
    }
    let host = item.get("HostConfig")?;
    let config = item.get("Config")?;
    if host.get("NetworkMode")?.as_str()? != "none"
        || !host.get("ReadonlyRootfs")?.as_bool()?
        || config.get("User")?.as_str()? != "10001:10001"
        || host.get("Privileged")?.as_bool()?
        || host.get("CapDrop")?.as_array()? != &vec![json!("ALL")]
        || !host
            .get("CapAdd")
            .is_none_or(|caps| caps.is_null() || caps.as_array().is_some_and(Vec::is_empty))
        || host.get("PidsLimit")?.as_i64()? != 256
        || !host
            .get("SecurityOpt")?
            .as_array()?
            .iter()
            .any(|v| v.as_str() == Some("no-new-privileges"))
    {
        return None;
    }
    if destination == "/run/castor/ipc.sock"
        && source.ends_with("/ipc.sock")
        && host.get("Memory")?.as_u64()? == 536_870_912
        && host.get("NanoCpus")?.as_u64()? == 1_000_000_000
        && config
            .get("Env")?
            .as_array()?
            .iter()
            .any(|v| v.as_str() == Some("CASTOR_IPC_SOCKET=/run/castor/ipc.sock"))
        && config.get("Cmd")?.as_array()?.iter().any(|v| {
            v.as_str().is_some_and(|s| {
                s.contains("exec pi --extension /opt/castor/castor-pi-extension.js")
            })
        })
    {
        return Some("pi");
    }
    if destination == "/candidate"
        && item.get("Image")?.as_str()? == verifier_id
        && host.get("Memory")?.as_u64()? == 1_073_741_824
        && host.get("NanoCpus")?.as_u64()? == 2_000_000_000
        && host.get("MemorySwap")?.as_u64()? == 1_073_741_824
        && mount.get("Propagation")?.as_str()? == "rprivate"
        && host.pointer("/LogConfig/Type")?.as_str()? == "none"
        && host.get("IpcMode")?.as_str()? == "private"
        && config.get("WorkingDir")?.as_str()? == "/workspace"
        && config
            .get("Entrypoint")
            .is_none_or(|entry| entry.is_null() || entry.as_array().is_some_and(Vec::is_empty))
        && config.get("Cmd")?.as_array()?.first()?.as_str()? == "/bin/sh"
        && config.get("Cmd")?.as_array()?.get(1)?.as_str()? == "-c"
    {
        return Some("verifier");
    }
    None
}

struct Slot {
    docker: Docker,
    state: PathBuf,
    evidence: PathBuf,
    token: String,
    image: String,
    controller: Option<String>,
    admin: Vec<String>,
    children: Vec<String>,
    scratch: Option<String>,
    parent: Option<String>,
    state_source: Option<String>,
    verifier_id: String,
    allocated: bool,
    adapter: Option<Child>,
    adapter_dir: Option<tempfile::TempDir>,
    budget: Option<BudgetLedger>,
    result: Option<Value>,
    status: String,
    run_error: Option<String>,
    cleanup_errors: Vec<String>,
}

impl Slot {
    fn new(state: PathBuf, token: String, image: String, verifier_id: String) -> io::Result<Self> {
        let evidence = state.join("launcher");
        fs::create_dir(&evidence)?;
        fs::set_permissions(&evidence, fs::Permissions::from_mode(0o700))?;
        let controller_state = state.join("controller");
        fs::create_dir(&controller_state)?;
        fs::create_dir(controller_state.join("bridge"))?;
        Ok(Self {
            docker: Docker::default(),
            state,
            evidence,
            token,
            image,
            controller: None,
            admin: Vec::new(),
            children: Vec::new(),
            scratch: None,
            parent: None,
            state_source: None,
            verifier_id,
            allocated: false,
            adapter: None,
            adapter_dir: None,
            budget: None,
            result: None,
            status: "FAILED".into(),
            run_error: None,
            cleanup_errors: Vec::new(),
        })
    }

    fn create(&mut self, args: &[String], label: &str) -> io::Result<String> {
        let cidfile = self.evidence.join(format!("{label}.cid"));
        let mut command = vec![
            "create".into(),
            "--pull=never".into(),
            "--cidfile".into(),
            path_string(&cidfile)?,
        ];
        command.extend_from_slice(args);
        let output = self.docker.call(&command, Duration::from_secs(30), false);
        let recorded = fs::read_to_string(&cidfile)
            .ok()
            .map(|s| s.trim().to_owned());
        if let Some(id) = &recorded {
            full_cid(id)?;
            if label == "controller" {
                self.controller = Some(id.clone());
            } else {
                self.admin.push(id.clone());
            }
        }
        let output = output?;
        let id = recorded.ok_or_else(|| invalid("Docker create omitted CID file"))?;
        if !output.status.success() || String::from_utf8_lossy(&output.stdout).trim() != id {
            return Err(invalid("Docker create result disagrees with recorded CID"));
        }
        Ok(id)
    }

    fn helper(&mut self, request: Value, parent: Option<&str>) -> io::Result<(Value, Value)> {
        let label = format!("admin-{}", &random_token()?[..8]);
        let mut args = vec![
            "--network".into(),
            "none".into(),
            "--read-only".into(),
            "--cap-drop".into(),
            "ALL".into(),
            "--cap-add".into(),
            "SYS_CHROOT".into(),
            "--mount".into(),
            "type=bind,src=/,dst=/engine,readonly".into(),
            "--mount".into(),
            format!("type=bind,src={},dst=/state", path_string(&self.state)?),
        ];
        if let Some(parent) = parent {
            args.extend([
                "--mount".into(),
                format!("type=bind,src={parent},dst=/engine{parent}"),
            ]);
        }
        args.extend([
            "--entrypoint".into(),
            "/usr/local/bin/castor".into(),
            self.image.clone(),
            "__engine-helper".into(),
            request.to_string(),
        ]);
        let id = self.create(&args, &label)?;
        let result = (|| {
            let inspected = self
                .docker
                .inspect(&id)?
                .ok_or_else(|| invalid("admin disappeared"))?;
            let output = self.docker.call(
                &["start".into(), "-a".into(), id.clone()],
                Duration::from_secs(15),
                false,
            )?;
            let waited = self
                .docker
                .text(&["wait".into(), id.clone()], Duration::from_secs(15))?;
            if !output.status.success() || waited != "0" {
                return Err(io::Error::other(format!(
                    "Engine helper failed: {}",
                    String::from_utf8_lossy(&output.stderr)
                )));
            }
            let value: Value = serde_json::from_slice(&output.stdout).map_err(io::Error::other)?;
            Ok((value, inspected))
        })();
        self.remove_cid(&id)?;
        result
    }

    fn remove_cid(&self, id: &str) -> io::Result<()> {
        let output = self.docker.call(
            &["rm".into(), "-f".into(), full_cid(id)?.into()],
            Duration::from_secs(15),
            false,
        )?;
        if !output.status.success() || self.docker.inspect(id)?.is_some() {
            return Err(invalid("exact CID removal not proved"));
        }
        Ok(())
    }

    fn preflight_engine(&mut self) -> io::Result<()> {
        let root = self.docker.text(
            &[
                "info".into(),
                "--format".into(),
                "{{.DockerRootDir}}".into(),
            ],
            Duration::from_secs(15),
        )?;
        let (paths, inspected) =
            self.helper(json!({"operation":"canonical","paths":[root,"/run"]}), None)?;
        let paths = paths
            .as_array()
            .ok_or_else(|| invalid("invalid Engine canonical output"))?;
        let docker_root = paths
            .first()
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing DockerRootDir"))?;
        let parent = paths
            .get(1)
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing Engine scratch parent"))?;
        if overlaps(parent, docker_root) {
            return Err(invalid("Engine scratch overlaps DockerRootDir"));
        }
        let scratch = format!("{parent}/c-{}", &self.token[..12]);
        let state_source = mount_source(&inspected, "/state")?.to_owned();
        let (canonical, _) = self.helper(
            json!({"operation":"canonical","paths":[state_source]}),
            None,
        )?;
        let state_source = canonical
            .get(0)
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("invalid Engine state mapping"))?
            .to_owned();
        if [scratch.as_str(), docker_root, "/run"]
            .iter()
            .any(|p| overlaps(&state_source, p))
        {
            return Err(invalid(
                "persistent state overlaps Engine scratch or DockerRootDir",
            ));
        }
        if format!("{scratch}/castor-pi-gateway-XXXXXX/ipc.sock").len() > 107 {
            return Err(invalid("Engine scratch exceeds Unix socket path budget"));
        }
        self.parent = Some(parent.into());
        self.scratch = Some(scratch.clone());
        self.state_source = Some(state_source.clone());
        write_json(
            &self.evidence.join("engine-map.json"),
            &json!({"docker_root":docker_root,"scratch":scratch,"state_source":state_source}),
        )?;
        self.allocated = true; // uncertain helper allocation must retain rather than claim CLEAN
        let (proof, _) = self.helper(
            json!({"operation":"allocate","parent":parent,"scratch":scratch,"token":self.token}),
            Some(parent),
        )?;
        if proof.get("exists").and_then(Value::as_bool) != Some(true) {
            return Err(invalid("Engine scratch allocation not proved"));
        }
        Ok(())
    }

    fn reserve_adapter_socket(&mut self) -> io::Result<PathBuf> {
        if self.adapter_dir.is_some() {
            return Err(invalid("host adapter socket already allocated"));
        }
        // Unix socket names are bounded independently of the user-selected
        // durable state root. mkdtemp gives this local socket a private 0700
        // parent; the directory is removed after the adapter exits.
        let dir = tempfile::Builder::new()
            .prefix("castor-model-")
            .tempdir_in("/tmp")?;
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700))?;
        let socket = dir.path().join("adapter.sock");
        if path_string(&socket)?.len() > 100 {
            return Err(invalid("host adapter socket path too long"));
        }
        self.adapter_dir = Some(dir);
        Ok(socket)
    }

    fn start_adapter(&mut self, script: &Path, pin: &Value) -> io::Result<PathBuf> {
        let socket = self.reserve_adapter_socket()?;
        let stdout = File::create(self.evidence.join("adapter.stdout"))?;
        let stderr = File::create(self.evidence.join("adapter.stderr"))?;
        let digest = pin
            .get("digest")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing model digest"))?;
        let child = Command::new("node")
            .arg(script)
            .arg(&socket)
            .env("CASTOR_LOCAL_DEVELOPER_MODE", "1")
            .env("CASTOR_EXPECTED_MODEL_DIGEST", digest)
            .stdout(stdout)
            .stderr(stderr)
            .spawn()?;
        self.adapter = Some(child);
        let until = Instant::now() + Duration::from_secs(5);
        while !socket.exists() {
            if self.adapter.as_mut().unwrap().try_wait()?.is_some() || Instant::now() >= until {
                return Err(invalid("local model adapter failed to start"));
            }
            thread::sleep(Duration::from_millis(10));
        }
        self.budget = Some(BudgetLedger::create(&self.evidence.join("model"))?);
        Ok(socket)
    }

    fn start_controller(&mut self, project: &Path, spec: &Path) -> io::Result<()> {
        let scratch = self
            .scratch
            .as_deref()
            .ok_or_else(|| invalid("missing Engine scratch"))?
            .to_owned();
        let spec_parent = spec
            .parent()
            .ok_or_else(|| invalid("missing task spec parent"))?;
        let spec_name = spec
            .file_name()
            .ok_or_else(|| invalid("missing task spec name"))?;
        let mounted_spec = Path::new("/spec").join(spec_name);
        let args = vec![
            "--network".into(),
            "none".into(),
            "--read-only".into(),
            "--cap-drop".into(),
            "ALL".into(),
            "--tmpfs".into(),
            "/tmp:rw,nosuid,nodev,size=64m".into(),
            "--env".into(),
            "HOME=/tmp".into(),
            "--env".into(),
            format!("TMPDIR={scratch}"),
            "--env".into(),
            format!("CASTOR_CONTROLLER_MODEL_SOCKET={scratch}/model.sock"),
            "--env".into(),
            "CASTOR_CONTROLLER_TIMEOUT_MS=300000".into(),
            "--env".into(),
            format!(
                "CASTOR_CONTROLLER_TASK_SPEC={}",
                path_string(&mounted_spec)?
            ),
            "--mount".into(),
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock".into(),
            "--mount".into(),
            format!("type=bind,src={scratch},dst={scratch}"),
            "--mount".into(),
            format!("type=bind,src={},dst=/state", path_string(&self.state)?),
            "--mount".into(),
            format!(
                "type=bind,src={},dst=/project,readonly",
                path_string(project)?
            ),
            "--mount".into(),
            format!(
                "type=bind,src={},dst=/spec,readonly",
                path_string(spec_parent)?
            ),
            self.image.clone(),
        ];
        let id = self.create(&args, "controller")?;
        let item = self
            .docker
            .inspect(&id)?
            .ok_or_else(|| invalid("controller disappeared before start"))?;
        if mount_source(&item, &scratch)? != scratch
            || mount_source(&item, "/state")?
                != self
                    .state_source
                    .as_deref()
                    .ok_or_else(|| invalid("missing state mapping"))?
        {
            return Err(invalid("controller Engine bind differs from preflight"));
        }
        self.docker
            .call(&["start".into(), id], Duration::from_secs(15), true)?;
        Ok(())
    }

    fn discover(&mut self) -> io::Result<Vec<Value>> {
        let Some(scratch) = self.scratch.as_deref() else {
            return Ok(Vec::new());
        };
        let mut live = Vec::new();
        for item in self.docker.inventory()? {
            let id = item
                .get("Id")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid("inventory without CID"))?;
            if self.controller.as_deref() == Some(id) || self.admin.iter().any(|known| known == id)
            {
                continue;
            }
            let private = item
                .get("Mounts")
                .and_then(Value::as_array)
                .is_some_and(|mounts| {
                    mounts.iter().any(|m| {
                        m.get("Source")
                            .and_then(Value::as_str)
                            .is_some_and(|source| inside(source, scratch))
                    })
                });
            if !private {
                continue;
            }
            if observed_child_profile(&item, scratch, &self.verifier_id).is_none() {
                return Err(invalid(
                    "unrecognized container uses private scratch; cleanup retained",
                ));
            }
            full_cid(id)?;
            if !self.children.iter().any(|known| known == id) {
                self.children.push(id.into());
            }
            live.push(item);
        }
        Ok(live)
    }

    fn workload(&mut self, adapter_socket: &Path) -> io::Result<()> {
        let deadline = Instant::now() + WORKLOAD;
        let bridge = self.state.join("controller/bridge");
        loop {
            self.budget
                .as_mut()
                .ok_or_else(|| invalid("missing budget ledger"))?
                .process_bridge_once(&bridge, |raw| {
                    forward_to_local_adapter(adapter_socket, raw, Duration::from_secs(125))
                })?;
            self.discover()?;
            let id = self
                .controller
                .as_deref()
                .ok_or_else(|| invalid("missing controller CID"))?;
            let item = self
                .docker
                .inspect(id)?
                .ok_or_else(|| invalid("controller disappeared before archive"))?;
            if item.pointer("/State/Running").and_then(Value::as_bool) == Some(false) {
                return Ok(());
            }
            if Instant::now() >= deadline {
                self.status = "TIMEOUT".into();
                return Ok(());
            }
            thread::sleep(Duration::from_millis(150));
        }
    }

    fn kill_if_running(&self, id: &str) -> io::Result<()> {
        if let Some(item) = self.docker.inspect(id)? {
            if item.pointer("/State/Running").and_then(Value::as_bool) == Some(true) {
                self.docker.call(
                    &[
                        "kill".into(),
                        "--signal".into(),
                        "SIGKILL".into(),
                        id.into(),
                    ],
                    Duration::from_secs(15),
                    true,
                )?;
            }
        }
        Ok(())
    }

    fn stop_recorded_controller(&self, id: &str) -> io::Result<()> {
        full_cid(id)?;
        // Even an inventory/inspect failure must not leave this exact,
        // privileged creator running. A stopped controller stays archived.
        let first = self.docker.inspect(id);
        if matches!(&first, Ok(Some(item)) if item.pointer("/State/Running").and_then(Value::as_bool) == Some(false))
        {
            return Ok(());
        }
        let attempted = self.docker.call(
            &[
                "kill".into(),
                "--signal".into(),
                "SIGKILL".into(),
                id.into(),
            ],
            Duration::from_secs(15),
            false,
        );
        let after = self.docker.inspect(id)?;
        if after
            .as_ref()
            .and_then(|item| item.pointer("/State/Running"))
            .and_then(Value::as_bool)
            == Some(false)
        {
            return Ok(());
        }
        Err(io::Error::other(format!(
            "recorded controller stop unproved (kill transport: {})",
            attempted
                .err()
                .map_or("returned".into(), |error| error.to_string())
        )))
    }

    fn archive_remove(&mut self, id: &str, controller: bool) -> io::Result<()> {
        let Some(item) = self.docker.inspect(id)? else {
            if controller {
                return Err(invalid("controller absent before archive"));
            }
            return Ok(());
        };
        if item.pointer("/State/Running").and_then(Value::as_bool) != Some(false) {
            return Err(invalid("container not terminal at archive"));
        }
        let output =
            self.docker
                .call(&["logs".into(), id.into()], Duration::from_secs(15), true)?;
        let name = if controller {
            "controller".into()
        } else {
            format!("child-{id}")
        };
        fs::write(self.evidence.join(format!("{name}.stdout")), &output.stdout)?;
        fs::write(self.evidence.join(format!("{name}.stderr")), &output.stderr)?;
        write_json(&self.evidence.join(format!("{name}.inspect.json")), &item)?;
        if controller && self.status != "TIMEOUT" {
            let result: Value = serde_json::from_slice(&output.stdout)
                .map_err(|_| invalid("native TaskResult missing or malformed"))?;
            if result.get("status").and_then(Value::as_str).is_none() {
                return Err(invalid("native TaskResult has no status"));
            }
            self.status = if result.get("status").and_then(Value::as_str) == Some("SUCCEEDED") {
                "SUCCEEDED"
            } else {
                "FAILED"
            }
            .into();
            write_json(&self.evidence.join("task-result.json"), &result)?;
            self.result = Some(result);
        }
        self.remove_cid(id)
    }

    fn finish(&mut self) -> Value {
        let deadline = Instant::now() + CLEANUP;
        self.docker.deadline = Some(deadline);
        if let Some(mut adapter) = self.adapter.take() {
            let _ = adapter.kill();
            if let Err(error) = adapter.wait() {
                self.cleanup_errors.push(error.to_string());
            }
        }
        if let Some(dir) = self.adapter_dir.take() {
            if let Err(error) = dir.close() {
                self.cleanup_errors.push(error.to_string());
            }
        }
        let cleanup = (|| -> io::Result<()> {
            if let Some(id) = self.controller.clone() {
                let before_stop = (|| -> io::Result<()> {
                    let live = self.discover()?;
                    for item in &live {
                        let cid = item
                            .get("Id")
                            .and_then(Value::as_str)
                            .ok_or_else(|| invalid("child CID missing"))?;
                        if item.pointer("/State/Running").and_then(Value::as_bool) == Some(true)
                            && item.pointer("/State/Paused").and_then(Value::as_bool) != Some(true)
                        {
                            self.docker.call(
                                &["pause".into(), cid.into()],
                                Duration::from_secs(15),
                                true,
                            )?;
                        }
                    }
                    Ok(())
                })();
                let stopped = self.stop_recorded_controller(&id);
                if let Err(error) = before_stop {
                    let proof = match &stopped {
                        Ok(()) => "proved".to_owned(),
                        Err(problem) => problem.to_string(),
                    };
                    return Err(io::Error::other(format!("private child discovery/pause failed: {error}; recorded controller stop: {proof}")));
                }
                stopped?;
                self.discover()?;
                for child in self.children.clone() {
                    self.kill_if_running(&child)?;
                }
                for child in self.children.clone() {
                    self.archive_remove(&child, false)?;
                }
                self.archive_remove(&id, true)?;
            }
            for id in self.admin.clone() {
                if self.docker.inspect(&id)?.is_some() {
                    self.remove_cid(&id)?;
                }
            }
            if self.allocated {
                let scratch = self
                    .scratch
                    .clone()
                    .ok_or_else(|| invalid("allocated scratch path missing"))?;
                for _ in 0..2 {
                    if self.docker.inventory()?.iter().any(|item| {
                        item.get("Mounts")
                            .and_then(Value::as_array)
                            .is_some_and(|mounts| {
                                mounts.iter().any(|m| {
                                    m.get("Source")
                                        .and_then(Value::as_str)
                                        .is_some_and(|s| s == scratch || inside(s, &scratch))
                                })
                            })
                    }) {
                        return Err(invalid("private scratch has remaining mounts"));
                    }
                    thread::sleep(Duration::from_millis(100));
                }
                let parent = self
                    .parent
                    .clone()
                    .ok_or_else(|| invalid("missing Engine scratch parent"))?;
                let (proof, _) = self.helper(json!({"operation":"remove","parent":parent,"scratch":scratch,"token":self.token}), Some(&parent))?;
                if proof.get("exists").and_then(Value::as_bool) != Some(false) {
                    return Err(invalid("scratch removal not proved"));
                }
            }
            if Instant::now() > deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "cleanup deadline exceeded",
                ));
            }
            Ok(())
        })();
        if let Err(error) = cleanup {
            self.cleanup_errors.push(error.to_string());
        }
        let clean = self.cleanup_errors.is_empty();
        let envelope = json!({
            "launcher_status": if !clean { "RETAINED_FAILURE" } else if self.run_error.is_some() { "LAUNCH_FAILED" } else { self.status.as_str() },
            "task_result": self.result,
            "cleanup_status": if clean { "CLEAN" } else { "RETAINED_FAILURE" },
            "run_error": self.run_error,
            "cleanup_errors": self.cleanup_errors,
            "evidence_dir": self.evidence,
            "controller_cid": self.controller,
            "owned_cids": self.children,
            "model_calls": self.budget.as_ref().map_or(0, BudgetLedger::count),
        });
        let _ = write_json(&self.evidence.join("final.json"), &envelope);
        envelope
    }
}

fn local_script(name: &str) -> io::Result<PathBuf> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or_else(|| invalid("missing source checkout root"))?;
    let path = root.join("kernel/carrier/pi/host").join(name);
    if !path.is_file() {
        return Err(invalid("source-checkout local model adapter missing"));
    }
    Ok(path)
}

fn controller_image(docker: &Docker) -> io::Result<String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or_else(|| invalid("missing source checkout root"))?;
    let dockerfile = root.join("kernel/controller/Dockerfile");
    if !dockerfile.is_file() {
        return Err(invalid("source-checkout controller Dockerfile missing"));
    }
    docker.call(
        &[
            "build".into(),
            "--pull=false".into(),
            "-f".into(),
            path_string(&dockerfile)?,
            "-t".into(),
            CONTROLLER_TAG.into(),
            path_string(root)?,
        ],
        Duration::from_secs(900),
        true,
    )?;
    let id = docker.text(
        &[
            "image".into(),
            "inspect".into(),
            "--format".into(),
            "{{.Id}}".into(),
            CONTROLLER_TAG.into(),
        ],
        Duration::from_secs(15),
    )?;
    let digest = id
        .strip_prefix("sha256:")
        .ok_or_else(|| invalid("invalid controller image ID"))?;
    full_cid(digest)?;
    Ok(id)
}

fn model_pin(script: &Path) -> io::Result<Value> {
    let mut command = Command::new("node");
    command.arg(script);
    let output = bounded_command(command, Duration::from_secs(15))?;
    if !output.status.success() {
        return Err(invalid("local Ollama model metadata unavailable"));
    }
    let pin: Value = serde_json::from_slice(&output.stdout).map_err(io::Error::other)?;
    if pin.get("name").and_then(Value::as_str) != Some("qwen3.5:9b") {
        return Err(invalid("local model identity mismatch"));
    }
    Ok(pin)
}

fn preflight_failure(state: &Path, error: &io::Error) -> io::Result<ExitCode> {
    let evidence = state.join("launcher");
    fs::create_dir(&evidence)?;
    fs::set_permissions(&evidence, fs::Permissions::from_mode(0o700))?;
    let envelope = json!({
        "launcher_status":"PREFLIGHT_FAILED", "task_result":null,
        "cleanup_status":"CLEAN", "cleanup_errors":[],
        "run_error":error.to_string(), "evidence_dir":evidence,
        "controller_cid":null, "owned_cids":[], "model_calls":0
    });
    write_json(&evidence.join("final.json"), &envelope)?;
    println!("{}", envelope);
    Ok(ExitCode::from(2))
}

pub fn run(project: &Path, spec: &Path, custom_state_root: Option<&Path>) -> io::Result<ExitCode> {
    let project = fs::canonicalize(project)?;
    let spec = fs::canonicalize(spec)?;
    let state_root = if let Some(root) = custom_state_root {
        root.to_owned()
    } else {
        PathBuf::from(env::var_os("HOME").ok_or_else(|| invalid("missing HOME"))?)
            .join(".castor/state")
    };
    fs::create_dir_all(&state_root)?;
    let state_root = fs::canonicalize(state_root)?;
    if state_root.starts_with(&project) {
        return Err(invalid("state root must be outside project"));
    }
    let token = random_token()?;
    let state = state_root.join(format!("run-{token}"));
    fs::create_dir(&state)?;
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
    // The pack validates the clean Git tree, task spec and pinned carrier image
    // before any provider process is started.
    let preflight = (|| -> io::Result<_> {
        if env::var_os("CASTOR_DEVELOPER_SOURCE_CHECKOUT").as_deref()
            == Some(std::ffi::OsStr::new("1"))
        {
            let receipt = pack_project(&project, &spec, &state.join("preflight-pack"))?;
            let pin = model_pin(&local_script("model_pin.mjs")?)?;
            let docker = Docker::default();
            let image = controller_image(&docker)?;
            let verifier = docker.text(&["image".into(), "inspect".into(), "--format".into(), "{{.Id}}".into(), "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea".into()], Duration::from_secs(15))?;
            return Ok((
                receipt,
                pin,
                image,
                verifier,
                local_script("ollama_model_adapter.mjs")?,
                json!(null),
            ));
        }
        let installed = InstalledRelease::load_current()?;
        let home = PathBuf::from(env::var_os("HOME").ok_or_else(|| invalid("missing HOME"))?);
        let prepared = revalidate(&installed, &DockerEngine, &home.join(".castor"))?;
        let carrier = format!("{}@{}", prepared.carrier_tag, prepared.carrier_id);
        let receipt =
            pack_project_with_carrier(&project, &spec, &state.join("preflight-pack"), &carrier)?;
        check_node_major()?;
        let checked = InstalledRelease::load_current()?;
        let pin = model_pin(checked.script(HostScript::ModelPin))?;
        let images = installed.pins(&prepared.engine_arch)?;
        let release = json!({"version":prepared.release_version,
            "source_revision":prepared.source_revision,
            "controller_ref":images.controller.reference,
            "carrier_ref":images.carrier.reference,
            "verifier_ref":images.verifier.reference,
            "controller_id":prepared.controller_id,
            "carrier_id":prepared.carrier_id,
            "verifier_id":prepared.verifier_id});
        Ok((
            receipt,
            pin,
            prepared.controller_id,
            prepared.verifier_id,
            installed.script(HostScript::OllamaAdapter).to_owned(),
            release,
        ))
    })();
    let (receipt, pin, image, verifier, adapter_script, release) = match preflight {
        Ok(value) => value,
        Err(error) => return preflight_failure(&state, &error),
    };
    write_json(
        &state.join("preflight.json"),
        &json!({"model_pin":pin,"controller_image":image,"verifier_image":verifier,"pack_receipt":receipt,"release":release}),
    )?;
    let mut slot = Slot::new(state, token, image, verifier)?;
    let run_result = (|| -> io::Result<()> {
        slot.preflight_engine()?;
        if env::var_os("CASTOR_DEVELOPER_SOURCE_CHECKOUT").as_deref()
            != Some(std::ffi::OsStr::new("1"))
        {
            InstalledRelease::load_current()?;
        }
        let adapter_socket = slot.start_adapter(&adapter_script, &pin)?;
        slot.start_controller(&project, &spec)?;
        slot.workload(&adapter_socket)
    })();
    if let Err(error) = run_result {
        slot.run_error = Some(error.to_string());
    }
    let envelope = slot.finish();
    println!(
        "{}",
        serde_json::to_string(&envelope).map_err(io::Error::other)?
    );
    let status = envelope.get("cleanup_status").and_then(Value::as_str);
    Ok(
        if status != Some("CLEAN")
            || envelope
                .get("run_error")
                .is_some_and(|value| !value.is_null())
        {
            ExitCode::from(2)
        } else if envelope.get("launcher_status").and_then(Value::as_str) == Some("SUCCEEDED") {
            ExitCode::SUCCESS
        } else {
            ExitCode::FAILURE
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    #[test]
    fn private_adapter_socket_stays_short_with_a_long_state_root() {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("s".repeat(96));
        fs::create_dir(&state).unwrap();
        let mut slot =
            Slot::new(state, "test-token".into(), "unused".into(), "unused".into()).unwrap();
        let socket = slot.reserve_adapter_socket().unwrap();
        assert!(path_string(&socket).unwrap().len() <= 100);
        assert!(!socket.starts_with(&slot.state));
        assert_eq!(
            fs::metadata(socket.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        let private_dir = socket.parent().unwrap().to_path_buf();
        let result = slot.finish();
        assert_eq!(result["cleanup_status"], "CLEAN");
        assert!(!private_dir.exists());
    }

    #[test]
    fn cid_and_private_child_bounds() {
        assert!(full_cid(&"a".repeat(64)).is_ok());
        assert!(full_cid(&"A".repeat(64)).is_err());
        assert!(inside("/run/c-abc/file", "/run/c-abc"));
        assert!(!inside("/run/c-abcd/file", "/run/c-abc"));
        assert!(!inside("/run/c-abc/../../etc", "/run/c-abc"));
        assert!(overlaps("/run", "/run/c-abc"));
    }

    #[test]
    #[ignore = "requires a local Docker Engine and built controller image"]
    fn engine_mapping_allocates_and_cleans_exact_scratch_without_a_model() {
        let root = tempfile::tempdir().unwrap();
        let state = fs::canonicalize(root.path()).unwrap().join("state");
        fs::create_dir(&state).unwrap();
        let docker = Docker::default();
        let image = docker
            .text(
                &[
                    "image".into(),
                    "inspect".into(),
                    "--format".into(),
                    "{{.Id}}".into(),
                    CONTROLLER_TAG.into(),
                ],
                Duration::from_secs(15),
            )
            .unwrap();
        let mut slot = Slot::new(state, random_token().unwrap(), image, "unused".into()).unwrap();
        slot.preflight_engine().unwrap();
        let final_result = slot.finish();
        assert_eq!(final_result["cleanup_status"], "CLEAN", "{final_result}");
    }

    #[test]
    #[ignore = "requires local Node runtime; starts adapter socket without a model call"]
    fn physical_long_state_root_starts_local_adapter_and_cleans() {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("s".repeat(96));
        fs::create_dir(&state).unwrap();
        let mut slot =
            Slot::new(state, "test-token".into(), "unused".into(), "unused".into()).unwrap();
        let socket = slot
            .start_adapter(
                &local_script("ollama_model_adapter.mjs").unwrap(),
                &json!({"digest": "test-digest"}),
            )
            .unwrap();
        assert!(socket.exists());
        assert!(path_string(&socket).unwrap().len() <= 100);
        let private_dir = socket.parent().unwrap().to_path_buf();
        let result = slot.finish();
        assert_eq!(result["cleanup_status"], "CLEAN", "{result}");
        assert!(!private_dir.exists());
    }

    #[test]
    #[ignore = "requires Docker Desktop and local pinned carrier/verifier images; fake model only"]
    fn physical_controller_exchanges_with_fake_model_and_cleans() {
        physical_controller_case(false);
    }

    #[test]
    #[ignore = "requires Docker Desktop and local pinned carrier/verifier images; fake model only"]
    fn physical_truncated_model_response_reports_limit_and_cleans() {
        physical_controller_case(true);
    }

    fn physical_controller_case(truncated: bool) {
        let root = tempfile::tempdir_in("/private/tmp").unwrap();
        let project = root.path().join("project");
        fs::create_dir(&project).unwrap();
        fs::write(project.join("hello.txt"), "hello\n").unwrap();
        let git = |args: &[&str]| {
            let output = Command::new("git")
                .arg("-C")
                .arg(&project)
                .args(args)
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
        };
        git(&["init", "-q"]);
        git(&["add", "hello.txt"]);
        git(&[
            "-c",
            "user.name=Castor Test",
            "-c",
            "user.email=castor@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ]);
        let spec = root.path().join("task-spec.json");
        fs::write(&spec, br#"{"schema_version":1,"task_prompt":"Find the included file, read it and change hello to fixed.","verification_command":["/bin/sh","-c","test \"$(cat hello.txt)\" = \"fixed\""]}"#).unwrap();
        let state = root.path().join("s".repeat(96));
        fs::create_dir(&state).unwrap();
        let docker = Docker::default();
        let image = docker
            .text(
                &[
                    "image".into(),
                    "inspect".into(),
                    "--format".into(),
                    "{{.Id}}".into(),
                    CONTROLLER_TAG.into(),
                ],
                Duration::from_secs(15),
            )
            .unwrap();
        let verifier = docker.text(&["image".into(), "inspect".into(), "--format".into(), "{{.Id}}".into(), "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea".into()], Duration::from_secs(15)).unwrap();
        let mut slot = Slot::new(state, random_token().unwrap(), image, verifier).unwrap();
        let saw_read = Arc::new(AtomicBool::new(false));
        let mut adapter_dir_path = None;
        let result = (|| -> io::Result<()> {
            slot.preflight_engine()?;
            let socket = slot.reserve_adapter_socket()?;
            adapter_dir_path = socket.parent().map(Path::to_path_buf);
            let listener = UnixListener::bind(&socket)?;
            listener.set_nonblocking(true)?;
            slot.budget = Some(BudgetLedger::create(&slot.evidence.join("model"))?);
            let worker_saw_read = saw_read.clone();
            let fake = thread::spawn(move || {
                let until = Instant::now() + Duration::from_secs(15);
                let mut calls = 0;
                while Instant::now() < until && calls < 3 {
                    let Ok((mut stream, _)) = listener.accept() else {
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    };
                    let mut prefix = [0_u8; 4];
                    stream.read_exact(&mut prefix).unwrap();
                    let size = u32::from_be_bytes(prefix) as usize;
                    assert!(size < 2 * 1024 * 1024);
                    let mut raw = vec![0; size];
                    stream.read_exact(&mut raw).unwrap();
                    let request: Value = serde_json::from_slice(&raw).unwrap();
                    let id = request
                        .get("interaction_id")
                        .and_then(Value::as_str)
                        .unwrap();
                    if calls == 1 && String::from_utf8_lossy(&raw).contains("hello\\n") {
                        worker_saw_read.store(true, Ordering::SeqCst);
                    }
                    let body = if calls == 0 {
                        json!({"content":[{"type":"toolCall","id":"read-1","name":"castor_read_file","arguments":{"path":"hello.txt"}}],"stopReason":"toolUse","usage":{"input":7,"output":5}})
                    } else if calls == 1 && truncated {
                        json!({"content":[{"type":"text","text":""}],"stopReason":"length","usage":{"input":9,"output":512}})
                    } else if calls == 1 {
                        json!({"content":[{"type":"toolCall","id":"edit-1","name":"castor_edit_file","arguments":{"path":"hello.txt","edits":[{"oldText":"hello","newText":"fixed"}]}}],"stopReason":"toolUse","usage":{"input":9,"output":8}})
                    } else {
                        json!({"content":[{"type":"text","text":"Done."}],"stopReason":"stop","usage":{"input":9,"output":2}})
                    };
                    let content = serde_json::to_vec(&body).unwrap();
                    let response = serde_json::to_vec(&json!({"interaction_id":id,"observation_region_id":format!("region://model-observation/{id}"),"observation_digest":format!("sha256:{:x}",Sha256::digest(&content)),"content":content})).unwrap();
                    stream
                        .write_all(&(response.len() as u32).to_be_bytes())
                        .unwrap();
                    stream.write_all(&response).unwrap();
                    calls += 1;
                }
                calls
            });
            slot.start_controller(&project, &spec)?;
            let outcome = slot.workload(&socket);
            let _ = fake.join();
            outcome
        })();
        if let Err(error) = result {
            slot.run_error = Some(error.to_string());
        }
        let final_result = slot.finish();
        assert!(adapter_dir_path.is_some_and(|path| !path.exists()));
        assert!(
            saw_read.load(Ordering::SeqCst),
            "Pi must observe the listed source file through its existing read tool: {final_result}"
        );
        if final_result["model_calls"] == 0 {
            eprintln!(
                "controller stderr: {}",
                fs::read_to_string(slot.evidence.join("controller.stderr")).unwrap_or_default()
            );
            eprintln!(
                "controller stdout: {}",
                fs::read_to_string(slot.evidence.join("controller.stdout")).unwrap_or_default()
            );
        }
        assert_eq!(final_result["cleanup_status"], "CLEAN", "{final_result}");
        assert_eq!(final_result["model_calls"], 2, "{final_result}");
        if truncated {
            assert_eq!(
                final_result["task_result"]["status"], "FAILED",
                "{final_result}"
            );
            assert_eq!(
                final_result["task_result"]["failure_reason"], "MODEL_OUTPUT_LIMIT_EXCEEDED",
                "{final_result}"
            );
            assert_eq!(final_result["task_result"]["settled_actions_count"], 0);
            assert!(final_result["task_result"].get("patch_diff").is_none());
            let budget: Value =
                serde_json::from_slice(&fs::read(slot.evidence.join("model/budget.json")).unwrap())
                    .unwrap();
            assert_eq!(budget["reservations"][1]["output_tokens"], 512);
            assert_eq!(budget["reservations"][1]["status"], "COMPLETED");
        } else {
            assert_eq!(
                final_result["task_result"]["status"], "SUCCEEDED",
                "{final_result}"
            );
        }
        let first = fs::read_to_string(slot.evidence.join("model/request-1.raw")).unwrap();
        assert!(
            first.contains("castor-file-inventory"),
            "inventory must reach the Pi model context"
        );
    }

    #[test]
    #[ignore = "requires local Docker Engine; creates exact test CIDs and private scratch"]
    fn unknown_private_child_stops_recorded_controller_and_retains_scratch() {
        let root = tempfile::tempdir_in("/private/tmp").unwrap();
        let state = root.path().join("state");
        fs::create_dir(&state).unwrap();
        let docker = Docker::default();
        let image = docker
            .text(
                &[
                    "image".into(),
                    "inspect".into(),
                    "--format".into(),
                    "{{.Id}}".into(),
                    CONTROLLER_TAG.into(),
                ],
                Duration::from_secs(15),
            )
            .unwrap();
        let mut slot = Slot::new(
            state,
            random_token().unwrap(),
            image.clone(),
            "unused".into(),
        )
        .unwrap();
        slot.preflight_engine().unwrap();
        let scratch = slot.scratch.clone().unwrap();
        let controller = slot
            .create(
                &[
                    "--network".into(),
                    "none".into(),
                    "--mount".into(),
                    "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock".into(),
                    "--mount".into(),
                    format!("type=bind,src={scratch},dst={scratch}"),
                    "--entrypoint".into(),
                    "/bin/sh".into(),
                    image.clone(),
                    "-c".into(),
                    "sleep 100".into(),
                ],
                "controller",
            )
            .unwrap();
        slot.docker
            .call(
                &["start".into(), controller.clone()],
                Duration::from_secs(15),
                true,
            )
            .unwrap();
        let foreign = slot
            .docker
            .text(
                &[
                    "create".into(),
                    "--pull=never".into(),
                    "--network".into(),
                    "none".into(),
                    "--mount".into(),
                    format!("type=bind,src={scratch},dst=/unknown"),
                    "--entrypoint".into(),
                    "/bin/sh".into(),
                    image,
                    "-c".into(),
                    "sleep 100".into(),
                ],
                Duration::from_secs(15),
            )
            .unwrap();
        full_cid(&foreign).unwrap();
        slot.docker
            .call(
                &["start".into(), foreign.clone()],
                Duration::from_secs(15),
                true,
            )
            .unwrap();
        let result = slot.finish();
        let stopped = slot
            .docker
            .inspect(&controller)
            .unwrap()
            .unwrap()
            .pointer("/State/Running")
            .and_then(Value::as_bool);
        slot.docker.deadline = None;
        slot.remove_cid(&foreign).unwrap();
        slot.remove_cid(&controller).unwrap();
        let parent = slot.parent.clone().unwrap();
        let (proof, _) = slot
            .helper(
                json!({"operation":"remove","parent":parent,"scratch":scratch,"token":slot.token}),
                Some(&parent),
            )
            .unwrap();
        assert_eq!(proof["exists"], false);
        assert_eq!(stopped, Some(false), "{result}");
        assert_eq!(result["cleanup_status"], "RETAINED_FAILURE");
    }
}
