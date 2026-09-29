//! Host-controlled, fail-closed Docker verification of a staged candidate.

use crate::one_shot::manifest::TaskManifest;
use serde::Serialize;
use serde_json::Value;
use std::fs;
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

// Resolve the pinned image without relying on Engine short-name aliases.
pub(crate) const IMAGE: &str =
    "docker.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea";
const LOG_CAP: usize = 1_048_576;
const COPY_AND_EXEC: &str = "cp -R /candidate/. /workspace/ && chmod -R u+rwX /workspace && cd /workspace && exec /usr/bin/env -i -- PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin HOME=/workspace LANG=C.UTF-8 \"$@\"";

#[derive(Debug, Clone, Serialize)]
pub struct InspectedProfile {
    pub image_id: String,
    pub candidate_source: String,
    pub log_driver: String,
    pub pids_limit: i64,
    pub memory_bytes: u64,
    pub memory_swap_bytes: u64,
    pub nano_cpus: u64,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct CapturedLogs {
    pub stdout_bytes: u64,
    pub stderr_bytes: u64,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct VerifierEvidence {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inspected_profile: Option<InspectedProfile>,
    pub captured_logs: CapturedLogs,
    pub deadline_ms: u64,
    pub setup_ms: u64,
    pub inspect_ms: u64,
    pub teardown_ms: u64,
    pub container_removed: bool,
    pub terminal_running: Option<bool>,
    pub terminal_oom_killed: Option<bool>,
    pub terminal_exit_code: Option<i32>,
}

pub struct Verification {
    pub code: i32,
    pub reason: &'static str,
    pub evidence: VerifierEvidence,
}

struct BoundedOutput {
    bytes: Vec<u8>,
    truncated: bool,
}

trait ReadFd: Read + AsRawFd {}
impl<T: Read + AsRawFd> ReadFd for T {}

impl BoundedOutput {
    fn new() -> Self {
        Self {
            bytes: Vec::new(),
            truncated: false,
        }
    }
    fn append(&mut self, bytes: &[u8]) {
        let available = LOG_CAP.saturating_sub(self.bytes.len());
        self.bytes
            .extend_from_slice(&bytes[..bytes.len().min(available)]);
        self.truncated |= bytes.len() > available;
    }
}

fn kill_group(child: &mut Child) {
    // Every Docker CLI invocation leads its own process group. Descendants
    // holding stdout/stderr cannot keep a reader alive past the deadline.
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.wait();
}

fn invoke(
    docker: &str,
    args: &[&str],
    deadline: Instant,
) -> io::Result<(bool, BoundedOutput, BoundedOutput)> {
    let mut child = Command::new(docker)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()?;
    let mut pipes: [Box<dyn ReadFd>; 2] = [
        Box::new(child.stdout.take().unwrap()),
        Box::new(child.stderr.take().unwrap()),
    ];
    let mut output = [BoundedOutput::new(), BoundedOutput::new()];
    let mut open = [true, true];
    let mut status = None;
    loop {
        if Instant::now() >= deadline {
            kill_group(&mut child);
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "Docker operation deadline",
            ));
        }
        if status.is_none() {
            status = match child.try_wait() {
                Ok(status) => status,
                Err(error) => {
                    kill_group(&mut child);
                    return Err(error);
                }
            };
        }
        if status.is_some() && !open[0] && !open[1] {
            break;
        }
        let mut fds = [
            libc::pollfd {
                fd: pipes[0].as_raw_fd(),
                events: if open[0] {
                    libc::POLLIN | libc::POLLHUP
                } else {
                    0
                },
                revents: 0,
            },
            libc::pollfd {
                fd: pipes[1].as_raw_fd(),
                events: if open[1] {
                    libc::POLLIN | libc::POLLHUP
                } else {
                    0
                },
                revents: 0,
            },
        ];
        let remaining = deadline.saturating_duration_since(Instant::now());
        let timeout = remaining.as_millis().min(20) as i32;
        let polled = unsafe { libc::poll(fds.as_mut_ptr(), 2, timeout) };
        if polled < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            kill_group(&mut child);
            return Err(error);
        }
        for i in 0..2 {
            if !open[i] || fds[i].revents == 0 {
                continue;
            }
            if fds[i].revents & libc::POLLNVAL != 0 {
                kill_group(&mut child);
                return Err(io::Error::other("Docker output fd invalid"));
            }
            let mut buf = [0_u8; 8192];
            match pipes[i].read(&mut buf) {
                Ok(0) => open[i] = false,
                Ok(n) => output[i].append(&buf[..n]),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => {
                    kill_group(&mut child);
                    return Err(e);
                }
            }
        }
    }
    let [stdout, stderr] = output;
    Ok((status.unwrap().success(), stdout, stderr))
}

fn profile(
    inspect: &Value,
    expected_id: &str,
    candidate: &Path,
    name: &str,
    command: &[String],
) -> Option<InspectedProfile> {
    let host = inspect.get("HostConfig")?;
    let image_id = inspect.get("Image")?.as_str()?;
    let mounts = inspect.get("Mounts")?.as_array()?;
    let candidate_source = candidate.to_str()?;
    let inspected = InspectedProfile {
        image_id: image_id.to_owned(),
        candidate_source: candidate_source.to_owned(),
        log_driver: host.get("LogConfig")?.get("Type")?.as_str()?.to_owned(),
        pids_limit: host.get("PidsLimit")?.as_i64()?,
        memory_bytes: host.get("Memory")?.as_u64()?,
        memory_swap_bytes: host.get("MemorySwap")?.as_u64()?,
        nano_cpus: host.get("NanoCpus")?.as_u64()?,
    };
    let candidate_read_only = mounts.len() == 1
        && mounts.iter().any(|mount| {
            mount["Type"] == "bind"
                && mount["Source"] == candidate_source
                && mount["Destination"] == "/candidate"
                && mount["RW"] == false
                && mount["Propagation"] == "rprivate"
        });
    let empty_or_null = |v: &Value| v.is_null() || v.as_array().is_some_and(Vec::is_empty);
    let tmpfs = host.get("Tmpfs")?;
    let workspace = tmpfs.get("/workspace")?.as_str()?;
    let shm = tmpfs.get("/dev/shm")?.as_str()?;
    let root = tmpfs.get("/root")?.as_str()?;
    let opts = |s: &str, expected: &[&str]| {
        expected
            .iter()
            .all(|part| s.split(',').any(|actual| actual == *part))
    };
    let expected_command: Vec<&str> = ["/bin/sh", "-c", COPY_AND_EXEC, "castor-verifier"]
        .into_iter()
        .chain(command.iter().map(String::as_str))
        .collect();
    if inspected.pids_limit != 256
        || inspected.memory_bytes != 1_073_741_824
        || inspected.memory_swap_bytes != 1_073_741_824
        || inspected.nano_cpus != 2_000_000_000
        || host["ReadonlyRootfs"] != true
        || host["NetworkMode"] != "none"
        || host["IpcMode"] != "private"
        || !matches!(host["PidMode"].as_str(), Some("") | Some("private"))
        || host["Privileged"] != false
        || host.get("CapDrop")? != &serde_json::json!(["ALL"])
        || !empty_or_null(host.get("CapAdd")?)
        || host.get("SecurityOpt")? != &serde_json::json!(["no-new-privileges"])
        || !empty_or_null(host.get("Binds")?)
        || !empty_or_null(host.get("Devices")?)
        || !empty_or_null(host.get("DeviceRequests")?)
        || !empty_or_null(host.get("VolumesFrom")?)
        || !empty_or_null(host.get("ExtraHosts")?)
        || !empty_or_null(host.get("Links")?)
        || inspected.log_driver != "none"
        || image_id != expected_id
        || inspect["Config"]["Image"] != IMAGE
        || inspect["Name"] != format!("/{name}")
        || inspect["Config"]["Labels"]["castor.verifier.owner"]
            != name.trim_start_matches("castor-verifier-")
        || inspect["Config"]["User"] != "10001:10001"
        || inspect["Config"]["WorkingDir"] != "/workspace"
        || !empty_or_null(inspect.get("Config")?.get("Entrypoint")?)
        || inspect["Config"]["Cmd"] != serde_json::to_value(expected_command).ok()?
        || tmpfs.as_object()?.len() != 3
        || !opts(
            workspace,
            &["size=512m", "mode=0700", "uid=10001", "gid=10001"],
        )
        || !opts(shm, &["size=64m"])
        || !opts(root, &["size=1m", "mode=0500", "uid=10001", "gid=10001"])
        || !candidate_read_only
    {
        return None;
    }
    Some(inspected)
}

fn inspect(docker: &str, id: &str, deadline: Instant) -> Option<Value> {
    let (ok, out, _) = invoke(docker, &["inspect", id], deadline).ok()?;
    if !ok || out.truncated {
        return None;
    }
    let value: Value = serde_json::from_slice(&out.bytes).ok()?;
    value.as_array()?.first().cloned()
}

fn terminal_verdict(state: &Value, waited: i32) -> Option<&'static str> {
    if state.get("Status")?.as_str()? != "exited"
        || !state.get("Error")?.as_str()?.is_empty()
        || state.get("Running")?.as_bool()?
        || state.get("ExitCode")?.as_i64()? != i64::from(waited)
    {
        return None;
    }
    let oom = state.get("OOMKilled")?.as_bool()?;
    Some(if oom || waited >= 128 {
        "VerifierCrashed"
    } else if matches!(waited, 125..=127) {
        "VerifierUnavailable"
    } else if waited == 0 {
        "NONE"
    } else {
        "TEST_VERIFICATION_FAILED"
    })
}

pub struct IsolatedVerifier;

impl IsolatedVerifier {
    pub fn run(
        manifest: &TaskManifest,
        candidate: &Path,
        state_root: &Path,
        allow_test_opcodes: bool,
    ) -> Verification {
        let docker = if allow_test_opcodes {
            std::env::var("CASTOR_TEST_VERIFIER_DOCKER").unwrap_or_else(|_| "docker".into())
        } else {
            "docker".into()
        };
        Self::run_with_docker(manifest, candidate, state_root, allow_test_opcodes, &docker)
    }

    fn run_with_docker(
        manifest: &TaskManifest,
        candidate: &Path,
        state_root: &Path,
        allow_test_opcodes: bool,
        docker: &str,
    ) -> Verification {
        let started = Instant::now();
        let default_ms = if allow_test_opcodes { 5_000 } else { 300_000 };
        let deadline_ms = if allow_test_opcodes {
            std::env::var("CASTOR_TEST_VERIFIER_TIMEOUT_MS")
                .ok()
                .and_then(|v| v.parse::<u64>().ok())
        } else {
            None
        }
        .or_else(|| {
            manifest
                .verification_timeout_seconds
                .and_then(|s| s.checked_mul(1000))
        })
        .unwrap_or(default_ms)
        .clamp(1, 86_400_000);
        let deadline = started + Duration::from_millis(deadline_ms);
        let mut evidence = VerifierEvidence {
            inspected_profile: None,
            captured_logs: CapturedLogs::default(),
            deadline_ms,
            setup_ms: 0,
            inspect_ms: 0,
            teardown_ms: 0,
            container_removed: false,
            terminal_running: None,
            terminal_oom_killed: None,
            terminal_exit_code: None,
        };
        let mut code = 1;
        let mut reason = "VerifierUnavailable";
        let mut container_id = None;
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let mut nonce = [0_u8; 16];
        let identity =
            fs::File::open("/dev/urandom").and_then(|mut file| file.read_exact(&mut nonce));
        if identity.is_err() || fs::create_dir_all(state_root).is_err() {
            return Verification {
                code,
                reason,
                evidence,
            };
        }
        let name = format!(
            "castor-verifier-{}",
            nonce.iter().map(|b| format!("{b:02x}")).collect::<String>()
        );
        let owner_label = format!(
            "castor.verifier.owner={}",
            name.trim_start_matches("castor-verifier-")
        );
        let owned = match tempfile::tempdir_in(state_root) {
            Ok(dir) => dir,
            Err(_) => {
                return Verification {
                    code,
                    reason,
                    evidence,
                }
            }
        };
        let cidfile = owned.path().join("container.cid");
        let candidate = match fs::canonicalize(candidate) {
            Ok(path) if path.is_dir() => path,
            _ => {
                return Verification {
                    code,
                    reason,
                    evidence,
                }
            }
        };
        // Keep a bounded portion of the deadline available for cleanup.
        let reserve = Duration::from_millis((deadline_ms / 4).min(2_000));
        let work_deadline = deadline - reserve;
        let image_id = invoke(
            docker,
            &["image", "inspect", "--format", "{{.Id}}", IMAGE],
            work_deadline,
        )
        .ok()
        .filter(|(ok, out, _)| *ok && !out.truncated)
        .and_then(|(_, out, _)| String::from_utf8(out.bytes).ok())
        .map(|id| id.trim().to_owned())
        .filter(|id| {
            id.starts_with("sha256:")
                && id.len() == 71
                && id[7..].bytes().all(|b| b.is_ascii_hexdigit())
        });
        if image_id.is_none() {
            return Verification {
                code,
                reason: if Instant::now() >= work_deadline {
                    "VerifierTimeout"
                } else {
                    reason
                },
                evidence,
            };
        }

        let args = [
            "create",
            "--cidfile",
            cidfile.to_str().unwrap(),
            "--name",
            &name,
            "--label",
            &owner_label,
            "--init",
            "--log-driver",
            "none",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--network",
            "none",
            "--mount",
            &format!(
                "type=bind,source={},target=/candidate,readonly,bind-propagation=rprivate",
                candidate.display()
            ),
            "--tmpfs",
            "/workspace:size=512m,mode=0700,uid=10001,gid=10001",
            "--tmpfs",
            "/dev/shm:size=64m",
            "--tmpfs",
            "/root:size=1m,mode=0500,uid=10001,gid=10001",
            "--pids-limit",
            "256",
            "--memory",
            "1073741824",
            "--memory-swap",
            "1073741824",
            "--cpus",
            "2.0",
            "--workdir",
            "/workspace",
            "--ipc",
            "private",
            IMAGE,
            "/bin/sh",
            "-c",
            COPY_AND_EXEC,
            "castor-verifier",
        ];
        let mut create_args = args.to_vec();
        create_args.extend(manifest.verification_command.iter().map(String::as_str));
        let setup = invoke(docker, &create_args, work_deadline);
        evidence.setup_ms = started.elapsed().as_millis() as u64;
        if let Ok((false, _, error)) = &setup {
            stderr = error.bytes.clone();
            evidence.captured_logs.stderr_truncated = error.truncated;
        }
        let cid = fs::read_to_string(&cidfile)
            .ok()
            .map(|id| id.trim().to_owned())
            .filter(|id| id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit()));
        if let Ok((true, output, _)) = setup {
            let printed = String::from_utf8(output.bytes)
                .ok()
                .map(|id| id.trim().to_owned());
            if printed == cid {
                container_id = cid;
            }
        }
        if container_id.is_none() && Instant::now() >= work_deadline {
            reason = "VerifierTimeout";
        }
        if let Some(id) = container_id.as_deref() {
            let inspect_start = Instant::now();
            let before = inspect(docker, id, work_deadline);
            evidence.inspect_ms = inspect_start.elapsed().as_millis() as u64;
            if let Some(before) = before {
                evidence.inspected_profile = profile(
                    &before,
                    image_id.as_deref().unwrap(),
                    &candidate,
                    &name,
                    &manifest.verification_command,
                );
                if before["Id"] == id
                    && before["State"]["Status"] == "created"
                    && before["State"]["Error"] == ""
                    && before["State"]["Running"] == false
                    && evidence.inspected_profile.is_some()
                {
                    match invoke(docker, &["start", "-a", id], work_deadline) {
                        Ok((attached_ok, out, err)) => {
                            evidence.captured_logs = CapturedLogs {
                                stdout_bytes: out.bytes.len() as u64,
                                stderr_bytes: err.bytes.len() as u64,
                                stdout_truncated: out.truncated,
                                stderr_truncated: err.truncated,
                            };
                            stdout = out.bytes;
                            stderr = err.bytes;
                            let inspect_start = Instant::now();
                            let waited = invoke(docker, &["wait", id], work_deadline)
                                .ok()
                                .filter(|(ok, out, _)| *ok && !out.truncated)
                                .and_then(|(_, out, _)| String::from_utf8(out.bytes).ok())
                                .and_then(|s| s.trim().parse::<i32>().ok());
                            let after = inspect(docker, id, work_deadline);
                            evidence.inspect_ms += inspect_start.elapsed().as_millis() as u64;
                            if let (Some(waited), Some(after)) = (waited, after) {
                                let state = &after["State"];
                                evidence.terminal_running = state["Running"].as_bool();
                                evidence.terminal_oom_killed = state["OOMKilled"].as_bool();
                                evidence.terminal_exit_code = state["ExitCode"]
                                    .as_i64()
                                    .and_then(|c| i32::try_from(c).ok());
                                if after["Id"] == id
                                    && profile(
                                        &after,
                                        image_id.as_deref().unwrap(),
                                        &candidate,
                                        &name,
                                        &manifest.verification_command,
                                    )
                                    .is_some()
                                {
                                    if let Some(verdict) = terminal_verdict(state, waited)
                                        .filter(|_| waited != 0 || attached_ok)
                                    {
                                        code = waited;
                                        reason = verdict;
                                    }
                                }
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::TimedOut => {
                            reason = "VerifierTimeout"
                        }
                        _ => {}
                    }
                }
            }
        }
        if Instant::now() >= work_deadline && reason != "NONE" {
            reason = "VerifierTimeout";
        }
        let teardown = Instant::now();
        // The CID file is written by Docker even when create's output is lost.
        // Its unpredictable per-run name is our owned recovery handle.
        let cleanup_id = fs::read_to_string(&cidfile)
            .ok()
            .map(|id| id.trim().to_owned())
            .filter(|id| id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit()));
        if let Some(id) = cleanup_id.as_deref() {
            if reason == "VerifierTimeout" {
                let _ = invoke(docker, &["kill", "--signal", "SIGKILL", id], deadline);
            }
            evidence.container_removed =
                invoke(docker, &["rm", "-f", id], deadline).is_ok_and(|(ok, _, _)| ok);
        } else if let Some(partial) = inspect(docker, &name, deadline) {
            // A failed create may be a name collision. Verify the owner label
            // before ever removing a container by name.
            if partial["Name"] == format!("/{name}")
                && partial["Config"]["Labels"]["castor.verifier.owner"]
                    == name.trim_start_matches("castor-verifier-")
            {
                evidence.container_removed =
                    invoke(docker, &["rm", "-f", &name], deadline).is_ok_and(|(ok, _, _)| ok);
            }
        }
        evidence.teardown_ms = teardown.elapsed().as_millis() as u64;
        evidence.captured_logs.stdout_bytes = stdout.len() as u64;
        evidence.captured_logs.stderr_bytes = stderr.len() as u64;
        let logs_saved = fs::create_dir_all(state_root)
            .and_then(|()| fs::write(state_root.join("verifier_stdout.log"), &stdout))
            .and_then(|()| fs::write(state_root.join("verifier_stderr.log"), &stderr))
            .is_ok();
        if (!logs_saved || !evidence.container_removed && container_id.is_some())
            && reason != "VerifierTimeout"
        {
            reason = "VerifierUnavailable";
        }
        if reason == "NONE"
            && (!evidence.container_removed
                || evidence.terminal_running != Some(false)
                || evidence.terminal_oom_killed != Some(false)
                || evidence.terminal_exit_code != Some(0))
        {
            reason = "VerifierUnavailable";
        }
        if reason != "NONE" && code == 0 {
            code = 1;
        }
        Verification {
            code,
            reason,
            evidence,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;

    fn valid_inspect() -> Value {
        json!({
            "Name": "/castor-verifier-owned", "Image": "sha256:expected", "Config": {"Image": IMAGE, "User": "10001:10001", "WorkingDir":"/workspace", "Entrypoint":null, "Labels":{"castor.verifier.owner":"owned"}, "Cmd":["/bin/sh", "-c", COPY_AND_EXEC, "castor-verifier", "true"]},
            "Mounts": [{"Type":"bind", "Source":"/source", "Destination":"/candidate", "RW":false, "Propagation":"rprivate"}],
            "HostConfig": {
                "PidsLimit":256, "Memory":1073741824_u64, "MemorySwap":1073741824_u64, "NanoCpus":2000000000_u64,
                "ReadonlyRootfs":true, "NetworkMode":"none", "IpcMode":"private", "PidMode":"",
                "Privileged":false, "CapDrop":["ALL"], "CapAdd":null, "SecurityOpt":["no-new-privileges"],
                "Binds":null, "Devices":[], "DeviceRequests":null, "VolumesFrom":null, "ExtraHosts":null, "Links":null, "LogConfig":{"Type":"none"},
                "Tmpfs": {"/workspace":"size=512m,mode=0700,uid=10001,gid=10001", "/dev/shm":"size=64m", "/root":"size=1m,mode=0500,uid=10001,gid=10001"}
            }
        })
    }

    #[test]
    fn prestart_profile_rejects_missing_or_wrong_authority_fields() {
        let good = valid_inspect();
        let command = vec!["true".to_owned()];
        assert!(profile(
            &good,
            "sha256:expected",
            Path::new("/source"),
            "castor-verifier-owned",
            &command
        )
        .is_some());
        for (pointer, bad) in [
            ("/Image", json!("sha256:other")),
            ("/HostConfig/Privileged", json!(true)),
            ("/HostConfig/CapAdd", json!(["SYS_ADMIN"])),
            ("/HostConfig/NetworkMode", json!("bridge")),
            ("/HostConfig/IpcMode", json!("host")),
            ("/HostConfig/PidMode", json!("host")),
            ("/HostConfig/LogConfig/Type", json!("json-file")),
            ("/HostConfig/MemorySwap", json!(0)),
            ("/Mounts/0/RW", json!(true)),
            ("/Mounts/0/Source", json!("/other")),
        ] {
            let mut observed = good.clone();
            *observed.pointer_mut(pointer).unwrap() = bad;
            assert!(
                profile(
                    &observed,
                    "sha256:expected",
                    Path::new("/source"),
                    "castor-verifier-owned",
                    &command
                )
                .is_none(),
                "accepted {pointer}"
            );
        }
        let mut missing = good.clone();
        missing["HostConfig"]
            .as_object_mut()
            .unwrap()
            .remove("CapDrop");
        assert!(profile(
            &missing,
            "sha256:expected",
            Path::new("/source"),
            "castor-verifier-owned",
            &command
        )
        .is_none());
    }

    #[test]
    fn incomplete_or_inconsistent_terminal_evidence_never_succeeds() {
        let good = json!({"Status":"exited", "Error":"", "Running":false, "OOMKilled":false, "ExitCode":0});
        assert_eq!(terminal_verdict(&good, 0), Some("NONE"));
        for bad in [
            json!({"Status":"created", "Error":"", "Running":false, "OOMKilled":false, "ExitCode":0}),
            json!({"Status":"exited", "Error":"launch failed", "Running":false, "OOMKilled":false, "ExitCode":0}),
            json!({"Running":true, "OOMKilled":false, "ExitCode":0}),
            json!({"OOMKilled":false, "ExitCode":0}),
            json!({"Running":false, "ExitCode":0}),
            json!({"Running":false, "OOMKilled":false}),
            json!({"Running":false, "OOMKilled":false, "ExitCode":1}),
            json!({"Running":"false", "OOMKilled":false, "ExitCode":0}),
        ] {
            assert_eq!(terminal_verdict(&bad, 0), None, "accepted {bad}");
        }
        assert_eq!(
            terminal_verdict(
                &json!({"Status":"exited", "Error":"", "Running":false,"OOMKilled":true,"ExitCode":0}),
                0
            ),
            Some("VerifierCrashed")
        );
    }

    #[test]
    fn fake_docker_backend_rejects_prestart_and_terminal_faults() {
        let root = tempfile::tempdir().unwrap();
        let candidate = root.path().join("candidate");
        fs::create_dir(&candidate).unwrap();
        fs::write(candidate.join("defect.txt"), "candidate").unwrap();
        let id = "a".repeat(64);
        let image_id = format!("sha256:{id}");
        let backend = root.path().join("fake-docker.sh");
        let observed = root.path().join("inspect.json");
        let namefile = root.path().join("name");
        let start_marker = root.path().join("started");
        let attach_failed = root.path().join("attach-failed");
        let script = format!(
            r##"#!/bin/sh
case "$1" in
  image)
    # This engine store only indexes the fully-qualified pinned reference.
    test "$5" = 'docker.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea' || exit 1
    printf '%s\n' '{image_id}' ;;
  create)
    prev=''
    for arg in "$@"; do
      case "$prev" in
        --cidfile) cidfile="$arg" ;;
        --name) name="$arg" ;;
      esac
      prev="$arg"
    done
    printf '%s\n' '{id}' > "$cidfile"
    printf '%s\n' "$name" > '{namefile}'
    printf '%s\n' '{id}' ;;
  inspect)
    name=$(cat '{namefile}')
    token=${{name#castor-verifier-}}
    state=created
    if test -f '{start_marker}'; then state=exited; fi
    sed -e "s/OWNED_NAME/$name/g" -e "s/OWNED_TOKEN/$token/g" -e "s/STATE_STATUS/$state/g" '{observed}' ;;
  start) touch '{start_marker}'; test ! -f '{attach_failed}' ;;
  wait) printf '0\n' ;;
  rm) exit 0 ;;
  *) exit 1 ;;
esac
"##,
            namefile = namefile.display(),
            observed = observed.display(),
            start_marker = start_marker.display(),
            attach_failed = attach_failed.display()
        );
        fs::write(&backend, script).unwrap();
        fs::set_permissions(&backend, fs::Permissions::from_mode(0o755)).unwrap();
        let manifest = TaskManifest {
            task_id: "task-fake-verifier".into(),
            idempotency_key: "fake-verifier-key".into(),
            carrier_base_image: "unused".into(),
            workspace_snapshot_path: "unused".into(),
            workspace_snapshot_sha256: "unused".into(),
            task_prompt: "unused".into(),
            verification_command: vec!["true".into()],
            verification_timeout_seconds: Some(3),
            limits: None,
        };
        let mut inspection = valid_inspect();
        inspection["Id"] = json!(id);
        inspection["Image"] = json!(image_id);
        inspection["Name"] = json!("/OWNED_NAME");
        inspection["Config"]["Labels"]["castor.verifier.owner"] = json!("OWNED_TOKEN");
        inspection["Mounts"][0]["Source"] =
            json!(fs::canonicalize(&candidate).unwrap().to_str().unwrap());
        inspection["State"] =
            json!({"Status":"STATE_STATUS", "Error":"", "Running":false, "ExitCode":0});
        fs::write(
            &observed,
            serde_json::to_vec(&json!([inspection.clone()])).unwrap(),
        )
        .unwrap();
        let missing_terminal = IsolatedVerifier::run_with_docker(
            &manifest,
            &candidate,
            &root.path().join("state-1"),
            true,
            backend.to_str().unwrap(),
        );
        assert!(
            start_marker.exists(),
            "fake backend did not reach the terminal fault"
        );
        assert_eq!(missing_terminal.reason, "VerifierUnavailable");
        assert_eq!(missing_terminal.evidence.terminal_oom_killed, None);
        fs::remove_file(&start_marker).unwrap();
        inspection["HostConfig"]["NetworkMode"] = json!("bridge");
        fs::write(
            &observed,
            serde_json::to_vec(&json!([inspection.clone()])).unwrap(),
        )
        .unwrap();
        let bad_prestart = IsolatedVerifier::run_with_docker(
            &manifest,
            &candidate,
            &root.path().join("state-2"),
            true,
            backend.to_str().unwrap(),
        );
        assert_eq!(bad_prestart.reason, "VerifierUnavailable");
        assert!(
            !start_marker.exists(),
            "user command started after a bad pre-start profile"
        );
        inspection["HostConfig"]["NetworkMode"] = json!("none");
        inspection["State"]["OOMKilled"] = json!(false);
        fs::write(&observed, serde_json::to_vec(&json!([inspection])).unwrap()).unwrap();
        fs::write(&attach_failed, "injected attach failure").unwrap();
        let failed_attach = IsolatedVerifier::run_with_docker(
            &manifest,
            &candidate,
            &root.path().join("state-3"),
            true,
            backend.to_str().unwrap(),
        );
        assert!(start_marker.exists());
        assert_eq!(failed_attach.reason, "VerifierUnavailable");
        fs::remove_file(&attach_failed).unwrap();
        fs::remove_file(&start_marker).unwrap();
        let complete = IsolatedVerifier::run_with_docker(
            &manifest,
            &candidate,
            &root.path().join("state-4"),
            true,
            backend.to_str().unwrap(),
        );
        assert_eq!(complete.reason, "NONE");
        assert_eq!(complete.code, 0);
        assert!(complete.evidence.container_removed);
    }
}
