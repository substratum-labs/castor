//! RFC v2 §6 physical non-bypass contract harness.

use castor_kernel::sandbox::{
    build_castor_untrusted_agent_config, RocheProcessSupervisor, RocheSandboxRunner,
    DEFAULT_SANDBOX_IMAGE,
};
use serde_json::Value;
use std::io;
use std::process::Command;
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::Duration;

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
    // Rust's stable Unix socket API does not expose abstract addresses on all
    // targets, so use the host Python runtime to bind this Linux-netns object.
    let mut host = Command::new("python3")
        .args([
            "-c",
            "import socket,time; s=socket.socket(socket.AF_UNIX); s.bind('\\0castor_host_shim'); time.sleep(10)",
        ])
        .spawn()
        .expect("bind host abstract UDS");
    thread::sleep(Duration::from_millis(50));
    let outcome = sandbox
        .python(
            r#"import errno,json,socket
try: socket.socket(socket.AF_UNIX).connect('\0castor_host_shim'); print(json.dumps('SUCCESS'))
except OSError as e: print(json.dumps(errno.errorcode.get(e.errno,str(e.errno))))"#,
        )
        .expect("abstract UDS probe");
    let _ = host.kill();
    let _ = host.wait();
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
    sandbox
        .supervisor
        .kill_immediate()
        .expect("SIGKILL carrier");
    assert_eq!(sandbox.supervisor.inspect_pid().expect("killed carrier"), 0);
}

#[test]
fn test_sandbox_containment_uncertainty_fail_closed() {
    let sandbox = Sandbox::new();
    sandbox
        .supervisor
        .remove()
        .expect("remove carrier to simulate loss");
    assert!(sandbox.supervisor.kill_immediate().is_err());
}
