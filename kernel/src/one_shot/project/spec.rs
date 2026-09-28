use super::invalid;
use super::io::{open_root_dir, read_bounded};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::io;
use std::path::{Component, Path, PathBuf};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskSpec {
    pub schema_version: u32,
    pub task_prompt: String,
    pub verification_command: Vec<String>,
    pub verification_timeout_seconds: Option<u64>,
    pub task_id: Option<String>,
    pub idempotency_key: Option<String>,
    pub exclude_paths: Option<Vec<String>>,
    pub verification_assets: Option<PathBuf>,
    pub limits: Option<serde_json::Value>,
}

fn safe_relative(path: &Path) -> bool {
    !path.as_os_str().is_empty()
        && !path.is_absolute()
        && path
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
}

pub fn parse_and_validate_spec(path: &Path) -> io::Result<TaskSpec> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let name = path
        .file_name()
        .ok_or_else(|| invalid("invalid task spec path"))?;
    let root = open_root_dir(parent)?;
    let (bytes, _) = read_bounded(&root, Path::new(name), 1024 * 1024)?;
    let spec: TaskSpec = serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
    if spec.schema_version != 1
        || spec.task_prompt.is_empty()
        || spec.verification_command.is_empty()
        || spec.verification_command[0].is_empty()
        || spec.verification_command.iter().any(|v| v.contains('\0'))
        || spec
            .verification_timeout_seconds
            .is_some_and(|n| !(1..=86_400).contains(&n))
        || spec.limits.is_some()
        || spec.task_id.is_some() != spec.idempotency_key.is_some()
    {
        return Err(invalid("invalid task specification"));
    }
    if let (Some(id), Some(key)) = (&spec.task_id, &spec.idempotency_key) {
        if !id.starts_with("task-")
            || id.len() <= 5
            || !id[5..]
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            || key.len() < 8
        {
            return Err(invalid("invalid explicit task identity"));
        }
    }
    if let Some(paths) = &spec.exclude_paths {
        for value in paths {
            let trimmed = value.strip_suffix('/').unwrap_or(value);
            if !safe_relative(Path::new(trimmed)) || value.contains('\0') || value.contains("//") {
                return Err(invalid("invalid exclude path"));
            }
        }
    }
    if let Some(assets) = &spec.verification_assets {
        if !safe_relative(assets) {
            return Err(invalid("invalid verification_assets path"));
        }
    }
    Ok(spec)
}

pub fn canonical_spec_bytes(spec: &TaskSpec) -> Vec<u8> {
    // Serialize fields in the frozen order, without deriving a map's key order.
    let mut s = format!(
        "{{\"schema_version\":1,\"task_prompt\":{},\"verification_command\":{},\"verification_timeout_seconds\":{}",
        serde_json::to_string(&spec.task_prompt).unwrap(),
        serde_json::to_string(&spec.verification_command).unwrap(),
        spec.verification_timeout_seconds.unwrap_or(300)
    );
    if let Some(paths) = &spec.exclude_paths {
        s.push_str(",\"exclude_paths\":");
        s.push_str(&serde_json::to_string(paths).unwrap());
    }
    if let Some(path) = &spec.verification_assets {
        s.push_str(",\"verification_assets\":");
        s.push_str(&serde_json::to_string(&path.to_string_lossy()).unwrap());
    }
    s.push('}');
    s.into_bytes()
}

pub fn derive_task_identity(
    spec: &TaskSpec,
    commit: &str,
    snapshot_sha256: &str,
    carrier_base_image: &str,
    verifier_pin: &str,
) -> (String, String) {
    let mut hasher = Sha256::new();
    hasher.update(b"v1:");
    hasher.update(commit.as_bytes());
    hasher.update(b":");
    hasher.update(snapshot_sha256.as_bytes());
    hasher.update(b":");
    hasher.update(canonical_spec_bytes(spec));
    hasher.update(b":");
    hasher.update(carrier_base_image.as_bytes());
    hasher.update(b":");
    hasher.update(verifier_pin.as_bytes());
    let hash = format!("{:x}", hasher.finalize());
    (format!("task-pack-{}", &hash[..16]), format!("pack-{hash}"))
}
