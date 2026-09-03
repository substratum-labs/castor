//! RFC v2 §6 physical non-bypass contract harness.
//!
//! These are intentionally RED in Phase 2.  They never replace the Roche
//! carrier with a mock: a host without a working Docker isolation provider
//! fails closed as `IsolationUnavailable`, and a host with Docker fails at the
//! intentionally absent Phase 3 Castor--Roche lifecycle bridge.

use castor_kernel::sandbox::build_castor_untrusted_agent_config;
use std::path::Path;
use std::process::Command;

const IPC_SOCKET: &str = "/run/castor/ipc.sock";
const TCP_ALLOWED_ERRNOS: &[&str] = &[
    "ENETUNREACH",
    "EHOSTUNREACH",
    "EACCES",
    "EPERM",
    "EAFNOSUPPORT",
];
const TCP_FORBIDDEN_ERRNOS: &[&str] = &["ECONNREFUSED", "ETIMEDOUT", "ECONNRESET"];

#[derive(Debug)]
struct IsolationUnavailable;

impl std::fmt::Display for IsolationUnavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("IsolationUnavailable: Docker/container isolation provider unavailable")
    }
}

/// Phase 3 replaces this gate with a real `castord` + Roche carrier fixture.
/// Its intentionally failing behavior prevents a non-isolated machine from
/// reporting any of these security properties as passing evidence.
fn require_real_phase3_carrier(scenario: &str) -> ! {
    let available = Command::new("docker")
        .args(["info", "--format", "{{.ServerVersion}}"])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false);
    if !available {
        panic!("{IsolationUnavailable}");
    }
    panic!(
        "Phase 3 Castor-Roche carrier integration is unavailable; {scenario} cannot substitute a mock"
    );
}

fn phase3_profile() {
    let config = build_castor_untrusted_agent_config(
        "alpine:3.20".to_owned(),
        Path::new("/private/var/run/castor-contract-ipc.sock"),
        None,
        None,
    )
    .expect("the dedicated socket fixture path must produce a native Roche profile");
    assert!(
        !config.network,
        "the physical carrier must use --network none"
    );
    assert_eq!(config.mounts.len(), 1);
    assert!(config.mounts[0].readonly);
    assert_eq!(config.mounts[0].container_path, IPC_SOCKET);
}

#[test]
fn test_sandbox_raw_tcp_egress_fails_closed() {
    phase3_profile();
    assert!(TCP_ALLOWED_ERRNOS.contains(&"ENETUNREACH"));
    assert!(!TCP_ALLOWED_ERRNOS
        .iter()
        .any(|errno| TCP_FORBIDDEN_ERRNOS.contains(errno)));
    require_real_phase3_carrier(
        "S1 must connect to 8.8.8.8:53, 1.1.1.1:80, and [2001:4860:4860::8888]:53, then inspect netns TX == 0",
    );
}

#[test]
fn test_sandbox_raw_udp_egress_fails_closed() {
    phase3_profile();
    require_real_phase3_carrier(
        "S2 must send raw UDP to 8.8.8.8:53 and 1.1.1.1:53, accepting only ENETUNREACH/EPERM with TX == 0",
    );
}

#[test]
fn test_sandbox_abstract_uds_isolated() {
    phase3_profile();
    require_real_phase3_carrier(
        "S3 must bind host abstract UDS @castor_host_shim and prove child connect fails across netns",
    );
}

#[test]
fn test_sandbox_vsock_egress_fails_closed() {
    phase3_profile();
    require_real_phase3_carrier(
        "S4 must attempt AF_VSOCK CID 2 and accept only EAFNOSUPPORT/EPERM with no /dev/vsock",
    );
}

#[test]
fn test_sandbox_host_filesystem_contained() {
    phase3_profile();
    require_real_phase3_carrier(
        "S5 must deny reads of a storage-root sentinel and /var/run/docker.sock with ENOENT/EACCES",
    );
}

#[test]
fn test_sandbox_socket_mount_readonly() {
    phase3_profile();
    require_real_phase3_carrier("S6 must reject unlink of /run/castor/ipc.sock with EROFS");
}

#[test]
fn test_sandbox_aisa_syscall_continuity() {
    phase3_profile();
    require_real_phase3_carrier(
        "S7 must AdmitTurn, RequestInteraction, and CommitTurn through /run/castor/ipc.sock with sandbox TX == 0",
    );
}

#[test]
fn test_sandbox_mediated_provider_io() {
    phase3_profile();
    require_real_phase3_carrier(
        "S8 must DeliverArmedAttempt via the host C-04 adapter while sandbox netns TX remains 0",
    );
}

#[test]
fn test_sandbox_fence_before_immediate_kill() {
    phase3_profile();
    require_real_phase3_carrier(
        "S9 must fsync GenerationFenced(2) before docker kill and reject stale generation admission",
    );
}

#[test]
fn test_sandbox_containment_uncertainty_fail_closed() {
    phase3_profile();
    require_real_phase3_carrier(
        "S10 must leave Armed attempts ArmedUnknown and reject new admission after carrier kill timeout",
    );
}
