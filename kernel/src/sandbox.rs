//! Castor's deliberately narrow Roche profile for untrusted Ring-3 agents.
//!
//! This module only constructs a native `roche_core::types::SandboxConfig`.
//! Starting a carrier, binding lifecycle requests, and granting semantic
//! authority remain Phase 3 responsibilities of `castord`.

use roche_core::types::{MountConfig, SandboxConfig};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::error::Error;
use std::fmt;
use std::path::{Component, Path, PathBuf};

const IPC_SOCKET_PATH: &str = "/run/castor/ipc.sock";
const STORAGE_LOCK_MARKER: &str = ".c01-writer.lock";

/// The Castor identity data carried with every physical lifecycle operation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LifecycleIdentityTuple {
    pub agent_id: String,
    pub agent_generation: u64,
    pub incarnation_id: String,
}

/// The only lifecycle observation classes Castor accepts from a carrier.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum LifecycleObservation {
    Observed,
    UnavailableOrUnknown,
    RejectedInvalidLifecycleRequest,
    ContainmentNotYetConfirmed,
}

/// Why an untrusted-agent sandbox profile could not be built safely.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SandboxConfigError {
    RelativeSocketPath { path: PathBuf },
    SensitiveHostPath { path: PathBuf },
    StorageRoot { path: PathBuf },
}

impl fmt::Display for SandboxConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::RelativeSocketPath { path } => {
                write!(
                    formatter,
                    "Castor IPC socket path must be absolute: {}",
                    path.display()
                )
            }
            Self::SensitiveHostPath { path } => write!(
                formatter,
                "Castor IPC socket path is inside a forbidden host path: {}",
                path.display()
            ),
            Self::StorageRoot { path } => write!(
                formatter,
                "Castor IPC socket path is inside a C-01 storage root: {}",
                path.display()
            ),
        }
    }
}

impl Error for SandboxConfigError {}

/// Builds the single-escape-hatch profile for an untrusted Castor agent.
///
/// There is intentionally no caller-controlled network setting: this
/// constructor always emits `network: false` and an empty allowlist.
pub fn build_castor_untrusted_agent_config(
    image: String,
    socket_host_path: &Path,
    memory_limit: Option<String>,
    cpu_limit: Option<f64>,
) -> Result<SandboxConfig, SandboxConfigError> {
    let socket_host_path = validate_socket_isolation(socket_host_path)?;
    let mut env = HashMap::new();
    env.insert("CASTOR_IPC_SOCKET".to_owned(), IPC_SOCKET_PATH.to_owned());

    Ok(SandboxConfig {
        provider: "docker".to_owned(),
        image,
        memory: memory_limit.or_else(|| Some("512m".to_owned())),
        cpus: cpu_limit.or(Some(1.0)),
        timeout_secs: 300,
        network: false,
        writable: false,
        env,
        mounts: vec![MountConfig {
            host_path: socket_host_path.to_string_lossy().into_owned(),
            container_path: IPC_SOCKET_PATH.to_owned(),
            readonly: true,
        }],
        kernel: None,
        rootfs: None,
        trace_enabled: true,
        network_allowlist: Vec::new(),
        fs_paths: Vec::new(),
    })
}

fn validate_socket_isolation(socket_host_path: &Path) -> Result<PathBuf, SandboxConfigError> {
    if !socket_host_path.is_absolute() {
        return Err(SandboxConfigError::RelativeSocketPath {
            path: socket_host_path.to_path_buf(),
        });
    }

    // `canonicalize` resolves an existing socket's symlink ancestors.  For a
    // not-yet-bound socket, retain a normalized lexical absolute path and scan
    // each existing ancestor for the C-01 lock marker below.
    let normalized = normalize_absolute_path(socket_host_path);
    let inspected = std::fs::canonicalize(socket_host_path).unwrap_or_else(|_| normalized.clone());
    for path in [&normalized, &inspected] {
        if is_sensitive_host_path(path) {
            return Err(SandboxConfigError::SensitiveHostPath { path: path.clone() });
        }
        if is_in_storage_root(path) {
            return Err(SandboxConfigError::StorageRoot { path: path.clone() });
        }
    }
    Ok(inspected)
}

fn normalize_absolute_path(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::from("/");
    for component in path.components() {
        match component {
            Component::RootDir => {}
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Normal(segment) => normalized.push(segment),
            Component::Prefix(_) => {}
        }
    }
    normalized
}

fn is_sensitive_host_path(path: &Path) -> bool {
    path == Path::new("/")
        || [
            "/etc",
            "/root",
            "/home",
            "/proc",
            "/sys",
            "/var/run/docker.sock",
        ]
        .iter()
        .any(|forbidden| path.starts_with(forbidden))
}

fn is_in_storage_root(path: &Path) -> bool {
    path.ancestors()
        .any(|ancestor| ancestor.join(STORAGE_LOCK_MARKER).exists())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn profile_is_networkless_and_mounts_only_the_readonly_ipc_socket() {
        let config = build_castor_untrusted_agent_config(
            "agent:latest".to_owned(),
            Path::new("/private/var/run/castor-ipc.sock"),
            None,
            None,
        )
        .expect("a dedicated socket path must be accepted");

        assert!(!config.network);
        assert!(!config.writable);
        assert_eq!(config.mounts.len(), 1);
        assert_eq!(config.mounts[0].container_path, "/run/castor/ipc.sock");
        assert!(config.mounts[0].readonly);
        assert_eq!(
            config.env.get("CASTOR_IPC_SOCKET"),
            Some(&"/run/castor/ipc.sock".to_owned())
        );
    }

    #[test]
    fn profile_rejects_socket_under_a_storage_root() {
        let root = tempfile::tempdir().expect("temporary storage root");
        std::fs::create_dir(root.path().join(".c01-writer.lock")).expect("create storage marker");

        let error = build_castor_untrusted_agent_config(
            "agent:latest".to_owned(),
            &root.path().join("ipc.sock"),
            None,
            None,
        )
        .expect_err("storage-root socket must be rejected");

        assert!(matches!(error, SandboxConfigError::StorageRoot { .. }));
    }
}
