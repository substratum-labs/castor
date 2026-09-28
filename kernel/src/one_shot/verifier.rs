//! Host-controlled, fail-closed Docker verification of a staged candidate.

use crate::one_shot::manifest::TaskManifest;
use serde::Serialize;
use serde_json::Value;
use std::fs;
use std::io::{self, Read};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const IMAGE: &str =
    "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea";
const LOG_CAP: usize = 1_048_576;
const ENV_PATH: &str = "PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin";
static NEXT_CONTAINER: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, Serialize)]
pub struct InspectedProfile {
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

fn drain(mut pipe: impl Read + Send + 'static) -> thread::JoinHandle<BoundedOutput> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let mut truncated = false;
        let mut buffer = [0; 8192];
        while let Ok(count) = pipe.read(&mut buffer) {
            if count == 0 {
                break;
            }
            let available = LOG_CAP.saturating_sub(bytes.len());
            bytes.extend_from_slice(&buffer[..count.min(available)]);
            truncated |= count > available;
        }
        BoundedOutput { bytes, truncated }
    })
}

fn wait_until(child: &mut Child, deadline: Instant) -> io::Result<bool> {
    loop {
        if child.try_wait()?.is_some() {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            child.kill()?;
            child.wait()?;
            return Ok(false);
        }
        thread::sleep(Duration::from_millis(10));
    }
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
        .spawn()?;
    let stdout = drain(child.stdout.take().expect("piped stdout"));
    let stderr = drain(child.stderr.take().expect("piped stderr"));
    let completed = wait_until(&mut child, deadline);
    if completed.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    let stdout = stdout
        .join()
        .map_err(|_| io::Error::other("stdout reader panicked"))?;
    let stderr = stderr
        .join()
        .map_err(|_| io::Error::other("stderr reader panicked"))?;
    let success = completed? && child.try_wait()?.is_some_and(|status| status.success());
    Ok((success, stdout, stderr))
}

fn profile(inspect: &Value) -> Option<InspectedProfile> {
    let host = inspect.get("HostConfig")?;
    let inspected = InspectedProfile {
        pids_limit: host.get("PidsLimit")?.as_i64()?,
        memory_bytes: host.get("Memory")?.as_u64()?,
        memory_swap_bytes: host.get("MemorySwap")?.as_u64()?,
        nano_cpus: host.get("NanoCpus")?.as_u64()?,
    };
    let candidate_read_only = inspect.get("Mounts")?.as_array()?.iter().any(|mount| {
        mount["Destination"] == "/candidate"
            && mount["RW"] == false
            && mount["Propagation"] == "rprivate"
    });
    if inspected.pids_limit != 256
        || inspected.memory_bytes != 1_073_741_824
        || inspected.memory_swap_bytes != 1_073_741_824
        || inspected.nano_cpus != 2_000_000_000
        || host["ReadonlyRootfs"] != true
        || host["NetworkMode"] != "none"
        || inspect["Config"]["User"] != "10001:10001"
        || !candidate_read_only
    {
        return None;
    }
    Some(inspected)
}

pub struct IsolatedVerifier;

impl IsolatedVerifier {
    pub fn run(
        manifest: &TaskManifest,
        candidate: &Path,
        state_root: &Path,
        allow_test_opcodes: bool,
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
        let docker = if allow_test_opcodes {
            std::env::var("CASTOR_TEST_VERIFIER_DOCKER").unwrap_or_else(|_| "docker".into())
        } else {
            "docker".into()
        };
        let mut evidence = VerifierEvidence {
            inspected_profile: None,
            captured_logs: CapturedLogs::default(),
            deadline_ms,
            setup_ms: 0,
            inspect_ms: 0,
            teardown_ms: 0,
            container_removed: false,
        };
        let mut code = 1;
        let mut reason = "VerifierUnavailable";
        let mut container_id = None;
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let name = format!(
            "castor-verifier-{}-{}",
            std::process::id(),
            NEXT_CONTAINER.fetch_add(1, Ordering::Relaxed)
        );

        let args = [
            "create",
            "--name",
            &name,
            "--init",
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
            IMAGE,
            "/usr/bin/env",
            "-i",
            ENV_PATH,
            "HOME=/workspace",
            "LANG=C.UTF-8",
        ];
        let mut create_args = args.to_vec();
        create_args.extend(manifest.verification_command.iter().map(String::as_str));
        let setup = invoke(&docker, &create_args, deadline);
        evidence.setup_ms = started.elapsed().as_millis() as u64;
        if let Ok((true, output, _)) = setup {
            let id = String::from_utf8_lossy(&output.bytes).trim().to_owned();
            if !id.is_empty() && id.bytes().all(|b| b.is_ascii_hexdigit()) {
                container_id = Some(id);
            }
        }
        if container_id.is_none() && Instant::now() >= deadline {
            reason = "VerifierTimeout";
        }
        if let Some(id) = container_id.as_deref() {
            let run = invoke(&docker, &["start", "-a", id], deadline);
            match run {
                Ok((_, out, err)) => {
                    evidence.captured_logs = CapturedLogs {
                        stdout_bytes: out.bytes.len() as u64,
                        stderr_bytes: err.bytes.len() as u64,
                        stdout_truncated: out.truncated,
                        stderr_truncated: err.truncated,
                    };
                    stdout = out.bytes;
                    stderr = err.bytes;
                    if Instant::now() >= deadline {
                        reason = "VerifierTimeout";
                        let _ = invoke(
                            &docker,
                            &["kill", "--signal", "SIGKILL", id],
                            Instant::now() + Duration::from_secs(2),
                        );
                    } else {
                        let inspect_start = Instant::now();
                        // `docker start -a` can detach before State has settled.
                        // `docker wait` supplies the terminal exit code, including
                        // nonzero test exits and signal deaths.
                        let waited = invoke(&docker, &["wait", id], deadline);
                        if let Ok((true, exit, _)) = waited {
                            code = String::from_utf8_lossy(&exit.bytes)
                                .trim()
                                .parse()
                                .unwrap_or(1);
                            if let Ok((true, data, _)) = invoke(&docker, &["inspect", id], deadline)
                            {
                                if let Ok(value) = serde_json::from_slice::<Value>(&data.bytes) {
                                    if let Some(obj) = value.as_array().and_then(|a| a.first()) {
                                        evidence.inspected_profile = profile(obj);
                                        let state = &obj["State"];
                                        reason = if evidence.inspected_profile.is_none()
                                            || code == 126
                                            || code == 127
                                        {
                                            "VerifierUnavailable"
                                        } else if state["OOMKilled"] == true || code >= 128 {
                                            "VerifierCrashed"
                                        } else if code == 0 {
                                            "NONE"
                                        } else {
                                            "TEST_VERIFICATION_FAILED"
                                        };
                                    }
                                }
                            }
                        }
                        evidence.inspect_ms = inspect_start.elapsed().as_millis() as u64;
                        if Instant::now() >= deadline {
                            reason = "VerifierTimeout";
                            let _ = invoke(
                                &docker,
                                &["kill", "--signal", "SIGKILL", id],
                                Instant::now() + Duration::from_secs(2),
                            );
                        }
                    }
                }
                Err(_) if Instant::now() >= deadline => {
                    reason = "VerifierTimeout";
                    let _ = invoke(
                        &docker,
                        &["kill", "--signal", "SIGKILL", id],
                        Instant::now() + Duration::from_secs(2),
                    );
                }
                Err(_) => {}
            }
        }
        let teardown = Instant::now();
        evidence.container_removed = invoke(
            &docker,
            &["rm", "-f", &name],
            teardown + Duration::from_secs(2),
        )
        .is_ok_and(|(ok, _, _)| ok);
        evidence.teardown_ms = teardown.elapsed().as_millis() as u64;
        let logs_saved = fs::create_dir_all(state_root)
            .and_then(|()| fs::write(state_root.join("verifier_stdout.log"), stdout))
            .and_then(|()| fs::write(state_root.join("verifier_stderr.log"), stderr))
            .is_ok();
        if !logs_saved || !evidence.container_removed && container_id.is_some() {
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
