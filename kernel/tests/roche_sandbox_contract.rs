//! RFC v2 §6 physical non-bypass contract harness.

use castor_kernel::sandbox::{
    build_castor_untrusted_agent_config, RocheProcessSupervisor, RocheSandboxRunner,
    DEFAULT_SANDBOX_IMAGE,
};
use castor_kernel::{
    c01_storage::{CoreEntry, D1DurableStorage},
    c02_execution::{D1ExecutionAuthority, ExecutionAuthority, ExecutionOutcome},
};
use serde_json::Value;
use std::io;
use std::process::Command;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Mutex, MutexGuard, OnceLock,
};
use std::thread;
use std::time::{Duration, Instant};

const TCP_ALLOWED_ERRNOS: &[&str] = &[
    "ENETUNREACH",
    "EHOSTUNREACH",
    "EACCES",
    "EPERM",
    "EAFNOSUPPORT",
];

struct Sandbox {
    _serial: MutexGuard<'static, ()>,
    root: tempfile::TempDir,
    supervisor: RocheProcessSupervisor,
}

struct DockerVmAbstractUdsHelper {
    container_name: String,
    socket_name: String,
}

impl DockerVmAbstractUdsHelper {
    fn new() -> Self {
        static NEXT_HELPER: AtomicU64 = AtomicU64::new(0);
        let unique = format!(
            "{}-{}",
            std::process::id(),
            NEXT_HELPER.fetch_add(1, Ordering::Relaxed)
        );
        let container_name = format!("roche-contract-s3-{unique}");
        let socket_name = format!("castor_host_shim_{unique}");
        let script = format!(
            "import socket,time; s=socket.socket(socket.AF_UNIX); s.bind('\\0{socket_name}'); print('READY', flush=True); time.sleep(60)"
        );
        let output = Command::new("docker")
            .args([
                "run",
                "--detach",
                "--name",
                &container_name,
                DEFAULT_SANDBOX_IMAGE,
            ])
            .args(["python3", "-c", &script])
            .output()
            .expect("start Docker VM abstract-UDS helper");
        assert!(
            output.status.success(),
            "Docker VM abstract-UDS helper failed to start: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let output = Command::new("docker")
                .args(["logs", &container_name])
                .output()
                .expect("read Docker VM helper logs");
            if output.status.success() && String::from_utf8_lossy(&output.stdout).contains("READY")
            {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "Docker VM abstract-UDS helper did not become ready: {}",
                String::from_utf8_lossy(&output.stderr)
            );
            thread::sleep(Duration::from_millis(25));
        }

        Self {
            container_name,
            socket_name,
        }
    }
}

impl Drop for DockerVmAbstractUdsHelper {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "--force", &self.container_name])
            .output();
    }
}

impl Sandbox {
    fn new() -> Self {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        let serial = LOCK
            .get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        // Docker Desktop shares the workspace but not macOS's /var/folders
        // temporary directory with its Linux VM.
        let root = tempfile::Builder::new()
            .prefix("roche-contract-")
            .tempdir_in(env!("CARGO_MANIFEST_DIR"))
            .expect("temporary IPC root in Docker-shared workspace");
        let socket = root.path().join("ipc.sock");
        // Docker Desktop's shared-files implementation cannot project a macOS
        // Unix-domain socket inode into its Linux VM.  The carrier API still
        // mounts this *single file* read-only; Linux CI supplies the real UDS
        // fixture through castord's gateway suite.
        std::fs::write(&socket, []).expect("create dedicated IPC mount source");
        let config = build_castor_untrusted_agent_config(
            DEFAULT_SANDBOX_IMAGE.to_owned(),
            &socket,
            None,
            None,
        )
        .expect("build Roche profile");
        let supervisor = RocheSandboxRunner::new(config)
            .start("exec sleep 60")
            .expect("start real Docker network-none carrier");
        Self {
            _serial: serial,
            root,
            supervisor,
        }
    }

    fn python(&self, script: &str) -> io::Result<Value> {
        let output = Command::new("docker")
            .args([
                "exec",
                self.supervisor.container_id(),
                "python3",
                "-c",
                script,
            ])
            .output()?;
        assert!(
            output.status.success(),
            "sandbox probe failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).map_err(io::Error::other)
    }

    fn assert_no_tx(&self) {
        assert_eq!(
            self.supervisor.inspect_netns_tx().expect("read netns TX"),
            0
        );
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = self.supervisor.remove();
    }
}

fn errno(value: &Value) -> &str {
    value.as_str().expect("errno string")
}

#[test]
fn test_sandbox_docker_hardening_options_are_applied() {
    let sandbox = Sandbox::new();
    let output = Command::new("docker")
        .args([
            "inspect",
            "--format",
            "{{.HostConfig.PidsLimit}}|{{json .HostConfig.SecurityOpt}}",
            sandbox.supervisor.container_id(),
        ])
        .output()
        .expect("inspect Docker hardening options");
    assert!(
        output.status.success(),
        "inspect Docker hardening options failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let hardening = String::from_utf8_lossy(&output.stdout);
    assert!(
        hardening.starts_with("256|"),
        "missing pids limit: {hardening}"
    );
    assert!(
        hardening.contains("no-new-privileges"),
        "missing no-new-privileges security option: {hardening}"
    );
}

#[test]
fn test_sandbox_raw_tcp_egress_fails_closed() {
    let sandbox = Sandbox::new();
    let outcome = sandbox.python(r#"import errno,json,socket
out=[]
for family,address in [(socket.AF_INET,('8.8.8.8',53)),(socket.AF_INET,('1.1.1.1',80)),(socket.AF_INET6,('2001:4860:4860::8888',53,0,0))]:
 try:
  s=socket.socket(family,socket.SOCK_STREAM); s.settimeout(.2); s.connect(address); out.append('SUCCESS')
 except OSError as e: out.append(errno.errorcode.get(e.errno,str(e.errno)))
print(json.dumps(out))"#).expect("TCP probe");
    for result in outcome.as_array().expect("TCP result list") {
        assert!(
            TCP_ALLOWED_ERRNOS.contains(&errno(result)),
            "unexpected TCP result: {result}"
        );
    }
    sandbox.assert_no_tx();
}

#[test]
fn test_sandbox_raw_udp_egress_fails_closed() {
    let sandbox = Sandbox::new();
    let outcome = sandbox
        .python(
            r#"import errno,json,socket
out=[]
for address in [('8.8.8.8',53),('1.1.1.1',53)]:
 try: socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b'x',address); out.append('SUCCESS')
 except OSError as e: out.append(errno.errorcode.get(e.errno,str(e.errno)))
print(json.dumps(out))"#,
        )
        .expect("UDP probe");
    for result in outcome.as_array().expect("UDP result list") {
        assert!(
            matches!(errno(result), "ENETUNREACH" | "EPERM"),
            "unexpected UDP result: {result}"
        );
    }
    sandbox.assert_no_tx();
}

#[test]
fn test_sandbox_abstract_uds_isolated() {
    let sandbox = Sandbox::new();
    // S3's peer must live in Docker's Linux VM, rather than the macOS host,
    // so this exercises namespace isolation at the same OS boundary as Roche.
    let helper = DockerVmAbstractUdsHelper::new();
    let script = format!(
        r#"import errno,json,socket
try: socket.socket(socket.AF_UNIX).connect('\0{}'); print(json.dumps('SUCCESS'))
except OSError as e: print(json.dumps(errno.errorcode.get(e.errno,str(e.errno))))"#,
        helper.socket_name
    );
    let outcome = sandbox.python(&script).expect("abstract UDS probe");
    assert_ne!(errno(&outcome), "SUCCESS", "abstract UDS crossed netns");
    sandbox.assert_no_tx();
}

#[test]
fn test_sandbox_vsock_egress_fails_closed() {
    let sandbox = Sandbox::new();
    let outcome = sandbox.python(r#"import errno,json,socket
try:
 family=getattr(socket,'AF_VSOCK'); s=socket.socket(family,socket.SOCK_STREAM); s.connect((2,1234)); print(json.dumps('SUCCESS'))
except (AttributeError,OSError) as e: print(json.dumps(errno.errorcode.get(getattr(e,'errno',None),'ATTRIBUTE_ERROR')))"#).expect("vsock probe");
    assert!(matches!(
        errno(&outcome),
        "EAFNOSUPPORT" | "EPERM" | "ATTRIBUTE_ERROR"
    ));
    assert_eq!(
        sandbox
            .python("import json,os; print(json.dumps(os.path.exists('/dev/vsock')))")
            .expect("/dev/vsock absence probe"),
        Value::Bool(false),
        "the carrier must not expose /dev/vsock"
    );
    sandbox.assert_no_tx();
}

#[test]
fn test_sandbox_host_filesystem_contained() {
    let sandbox = Sandbox::new();
    let sentinel = sandbox.root.path().join("host-sentinel");
    std::fs::write(&sentinel, b"host-only").expect("write sentinel");
    let script = format!(
        r#"import errno,json
out=[]
for path in [{:?},'/var/run/docker.sock']:
 try: open(path,'rb').read(); out.append('SUCCESS')
 except OSError as e: out.append(errno.errorcode.get(e.errno,str(e.errno)))
print(json.dumps(out))"#,
        sentinel
    );
    let outcome = sandbox.python(&script).expect("filesystem probe");
    for result in outcome.as_array().expect("filesystem result list") {
        assert!(
            matches!(errno(result), "ENOENT" | "EACCES"),
            "host file exposed: {result}"
        );
    }
}

#[test]
fn test_sandbox_socket_mount_readonly() {
    let sandbox = Sandbox::new();
    let outcome = sandbox
        .python(
            r#"import errno,json,os
try: os.unlink('/run/castor/ipc.sock'); print(json.dumps('SUCCESS'))
except OSError as e: print(json.dumps(errno.errorcode.get(e.errno,str(e.errno))))"#,
        )
        .expect("readonly mount probe");
    assert_eq!(errno(&outcome), "EROFS");
}

#[test]
fn test_sandbox_aisa_syscall_continuity() {
    let sandbox = Sandbox::new();
    let outcome = sandbox.python(r#"import json,os
print(json.dumps(os.environ.get('CASTOR_IPC_SOCKET') == '/run/castor/ipc.sock' and os.path.exists('/run/castor/ipc.sock')))"#).expect("AISA socket probe");
    assert_eq!(outcome, Value::Bool(true));
    sandbox.assert_no_tx();
}

#[test]
fn test_sandbox_mediated_provider_io() {
    let sandbox = Sandbox::new();
    let outcome = sandbox
        .python(
            r#"import json,os,stat
print(json.dumps(os.path.exists('/run/castor/ipc.sock')))"#,
        )
        .expect("mediation socket probe");
    assert_eq!(outcome, Value::Bool(true));
    sandbox.assert_no_tx();
}

#[test]
fn test_sandbox_fence_before_immediate_kill() {
    let sandbox = Sandbox::new();
    assert!(sandbox.supervisor.inspect_pid().expect("running PID") > 0);
    let durable_root = tempfile::tempdir().expect("temporary D1 authority root");
    {
        let storage = D1DurableStorage::open(durable_root.path()).expect("open D1 authority");
        let mut authority = D1ExecutionAuthority::for_ready_turn(
            storage,
            "roche-s9",
            1,
            1,
            1,
            1,
            "sha256:roche-s9-base",
        );
        assert_eq!(
            authority.revoke_or_fence_execution(1, 1, 2, 2),
            ExecutionOutcome::GenerationFenced {
                persisted_entry_id: 2,
                agent_generation: 2,
            }
        );
    }
    let recovered = D1DurableStorage::open(durable_root.path()).expect("reopen durable D1 journal");
    assert!(
        recovered
            .journal_requests()
            .iter()
            .any(|request| matches!(request.entry, CoreEntry::FenceRevoked { generation: 2 })),
        "GenerationFenced(2) must be durable before killing the carrier"
    );
    drop(recovered);
    sandbox
        .supervisor
        .kill_immediate()
        .expect("SIGKILL carrier");
    assert_eq!(sandbox.supervisor.inspect_pid().expect("killed carrier"), 0);
}

#[test]
fn test_sandbox_containment_uncertainty_fail_closed() {
    let sandbox = Sandbox::new();
    // S10 simulates carrier loss by removing it behind the supervisor's back;
    // the stale handle must still fail its attempted kill rather than report
    // containment as confirmed.
    sandbox
        .supervisor
        .remove()
        .expect("remove carrier to simulate loss");
    assert!(sandbox.supervisor.kill_immediate().is_err());
}
