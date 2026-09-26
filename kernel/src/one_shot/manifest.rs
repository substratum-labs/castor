use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskManifest {
    pub task_id: String,
    pub idempotency_key: String,
    pub carrier_base_image: String,
    pub workspace_snapshot_path: PathBuf,
    pub workspace_snapshot_sha256: String,
    pub task_prompt: String,
    pub verification_command: Vec<String>,
    #[serde(default)]
    pub limits: Option<serde_json::Value>,
}

pub struct ValidatedSnapshot {
    pub file: File,
    pub sha256: String,
}

impl TaskManifest {
    pub fn read(path: &Path) -> io::Result<Self> {
        let bytes = fs::read(path)?;
        let manifest: Self = serde_json::from_slice(&bytes)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        if !manifest.task_id.starts_with("task-")
            || !manifest.task_id[5..]
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
            || manifest.task_id.len() <= 5
            || manifest.idempotency_key.len() < 8
            || manifest.verification_command.is_empty()
            || manifest.verification_command[0].is_empty()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid one-shot task manifest",
            ));
        }
        Ok(manifest)
    }

    pub fn validate_snapshot(&self, manifest_path: &Path) -> io::Result<ValidatedSnapshot> {
        let snapshot = &self.workspace_snapshot_path;
        if snapshot.is_absolute()
            || !snapshot
                .components()
                .all(|part| matches!(part, Component::Normal(_) | Component::CurDir))
        {
            return Err(invalid_snapshot(
                "snapshot path must be relative without traversal",
            ));
        }
        let name = snapshot
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| invalid_snapshot("snapshot archive name must be UTF-8"))?;
        if ![".tar", ".tar.gz", ".tgz", ".tar.zst"]
            .iter()
            .any(|suffix| name.ends_with(suffix))
        {
            return Err(invalid_snapshot("snapshot must be a supported archive"));
        }
        let parent = manifest_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let mut resolved = fs::canonicalize(parent)?;
        for component in snapshot.components() {
            if let Component::Normal(name) = component {
                resolved.push(name);
                let metadata = fs::symlink_metadata(&resolved)
                    .map_err(|_| invalid_snapshot("snapshot archive is missing"))?;
                if metadata.file_type().is_symlink() {
                    return Err(invalid_snapshot("snapshot path cannot contain symlinks"));
                }
            }
        }
        let metadata = fs::metadata(&resolved)?;
        if !metadata.is_file() {
            return Err(invalid_snapshot("snapshot must be a regular archive file"));
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&resolved)?;
        if !file.metadata()?.is_file() {
            return Err(invalid_snapshot("snapshot must remain a regular file"));
        }
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            hasher.update(&buffer[..count]);
        }
        let actual = format!("{:x}", hasher.finalize());
        if actual != self.workspace_snapshot_sha256 {
            return Err(invalid_snapshot("snapshot archive digest mismatch"));
        }
        file.seek(SeekFrom::Start(0))?;
        Ok(ValidatedSnapshot {
            file,
            sha256: actual,
        })
    }
}

fn invalid_snapshot(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
