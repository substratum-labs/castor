use crate::one_shot::manifest::ValidatedSnapshot;
use flate2::read::GzDecoder;
use serde_json::Value;
use std::collections::HashSet;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use tar::{Archive, Builder, Header};

const MAX_ENTRIES: usize = 50_000;
const MAX_EXTRACTED_BYTES: u64 = 1024 * 1024 * 1024;

pub struct StagedSnapshot {
    root: tempfile::TempDir,
}

struct StagingContainer(String);

impl Drop for StagingContainer {
    fn drop(&mut self) {
        if !self.0.is_empty() {
            let _ = Command::new("docker").args(["rm", &self.0]).output();
        }
    }
}

struct DerivedImage(String);

impl Drop for DerivedImage {
    fn drop(&mut self) {
        if !self.0.is_empty() {
            let _ = Command::new("docker")
                .args(["image", "rm", &self.0])
                .output();
        }
    }
}

fn docker(args: &[&str]) -> io::Result<String> {
    let output = Command::new("docker").args(args).output()?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "Docker {} failed: {}",
            args.first().copied().unwrap_or("?"),
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    String::from_utf8(output.stdout)
        .map(|value| value.trim().to_owned())
        .map_err(|_| invalid_archive("non-UTF8 Docker output"))
}

fn inspect_image(image: &str) -> io::Result<Value> {
    let output = docker(&["image", "inspect", image])?;
    let mut images: Vec<Value> = serde_json::from_str(&output).map_err(io::Error::other)?;
    if images.len() != 1 {
        return Err(invalid_archive("Docker image inspect was ambiguous"));
    }
    Ok(images.remove(0))
}

fn snapshot_tar(workspace: &Path, target: &Path) -> io::Result<()> {
    let output = fs::File::create(target)?;
    let mut tar = Builder::new(output);
    let mut pending = vec![workspace.to_owned()];
    let mut count = 0usize;
    let mut total = 0u64;
    while let Some(directory) = pending.pop() {
        let mut children = fs::read_dir(&directory)?
            .map(|entry| entry.map(|entry| entry.path()))
            .collect::<io::Result<Vec<_>>>()?;
        children.sort();
        for path in children {
            count += 1;
            if count > MAX_ENTRIES {
                return Err(invalid_archive("snapshot has too many entries"));
            }
            let name = path.strip_prefix(workspace).map_err(io::Error::other)?;
            let metadata = fs::symlink_metadata(&path)?;
            let mut header = Header::new_gnu();
            header.set_uid(10001);
            header.set_gid(10001);
            header.set_mode(0o555);
            header.set_mtime(0);
            if metadata.is_dir() {
                header.set_entry_type(tar::EntryType::Directory);
                header.set_size(0);
                header.set_cksum();
                tar.append_data(&mut header, name, io::empty())?;
                pending.push(path);
            } else if metadata.is_file() {
                total = total
                    .checked_add(metadata.len())
                    .ok_or_else(|| invalid_archive("snapshot size overflow"))?;
                if total > MAX_EXTRACTED_BYTES {
                    return Err(invalid_archive("snapshot exceeds size limit"));
                }
                header.set_entry_type(tar::EntryType::Regular);
                header.set_size(metadata.len());
                header.set_cksum();
                tar.append_data(&mut header, name, fs::File::open(&path)?)?;
            } else {
                return Err(invalid_archive("snapshot contains a link or special file"));
            }
        }
    }
    tar.finish()?;
    tar.into_inner()?.sync_all()
}

impl StagedSnapshot {
    pub fn stage(snapshot: ValidatedSnapshot, name: &Path) -> io::Result<Self> {
        let root = tempfile::Builder::new()
            .prefix("castor-task-image-")
            .tempdir()?;
        let workspace = root.path().join("workspace_snapshot");
        fs::create_dir(&workspace)?;
        let mut file = snapshot.file;
        file.seek(SeekFrom::Start(0))?;
        let name = name
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| invalid_archive("archive name is not UTF-8"))?;
        if name.ends_with(".tar.gz") || name.ends_with(".tgz") {
            unpack_archive(GzDecoder::new(file), &workspace)?;
        } else if name.ends_with(".tar.zst") {
            unpack_archive(zstd::stream::read::Decoder::new(file)?, &workspace)?;
        } else if name.ends_with(".tar") {
            unpack_archive(file, &workspace)?;
        } else {
            return Err(invalid_archive("unsupported snapshot archive"));
        }
        Ok(Self { root })
    }

    pub fn workspace(&self) -> PathBuf {
        self.root.path().join("workspace_snapshot")
    }

    pub fn build(&self, carrier_base_image: &str) -> io::Result<String> {
        validate_base_image(carrier_base_image)?;
        let (carrier_tag, expected_id) = carrier_base_image
            .split_once('@')
            .expect("validated carrier reference");
        let base = inspect_image(carrier_tag)?;
        if base.get("Id").and_then(Value::as_str) != Some(expected_id) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "local Pi carrier digest differs from task manifest pin",
            ));
        }
        if base.pointer("/Config/Volumes").is_some_and(|value| {
            !value.is_null() && value.as_object().is_some_and(|volumes| !volumes.is_empty())
        }) {
            return Err(invalid_archive(
                "carrier defines volumes; snapshot image would be incomplete",
            ));
        }
        let tar_path = self.root.path().join("snapshot.tar");
        snapshot_tar(&self.workspace(), &tar_path)?;
        let mut args = vec![
            "create",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
        ];
        let marker = std::env::var("CASTOR_STAGE_OWNER_DIR").ok();
        let token = std::env::var("CASTOR_STAGE_OWNER_TOKEN").ok();
        if marker.is_some() != token.is_some() {
            return Err(invalid_archive("incomplete staging ownership"));
        }
        if let Some(token) = &token {
            if token.len() != 32
                || !token
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            {
                return Err(invalid_archive("invalid staging owner token"));
            }
        }
        let label = token
            .as_ref()
            .map(|token| format!("castor.stage.owner={token}"));
        let mount = marker
            .as_ref()
            .map(|path| format!("type=bind,src={path},dst=/run/castor-stage-owner,readonly"));
        if let Some(value) = &mount {
            args.extend(["--mount", value]);
        }
        if let Some(value) = &label {
            args.extend(["--label", value]);
        }
        args.push(expected_id);
        let cid = docker(&args)?;
        if cid.len() != 64 || !cid.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(invalid_archive("Docker returned invalid staging CID"));
        }
        let mut staging = StagingContainer(cid);
        let created: Vec<Value> =
            serde_json::from_str(&docker(&["inspect", &staging.0])?).map_err(io::Error::other)?;
        let created = created
            .first()
            .ok_or_else(|| invalid_archive("staging inspect missing"))?;
        if created.get("Image").and_then(Value::as_str) != Some(expected_id)
            || created.pointer("/State/Status").and_then(Value::as_str) != Some("created")
            || created.pointer("/State/Running").and_then(Value::as_bool) != Some(false)
            || created
                .get("Mounts")
                .and_then(Value::as_array)
                .map(Vec::len)
                != Some(usize::from(marker.is_some()))
        {
            return Err(invalid_archive(
                "staging container differs from pinned, stopped profile",
            ));
        }
        let stage = Command::new("docker")
            .args(["cp", "-a", "-"])
            .arg(format!("{}:/workspace", staging.0))
            .stdin(Stdio::from(fs::File::open(&tar_path)?))
            .output()?;
        if !stage.status.success() {
            return Err(io::Error::other(format!(
                "snapshot copy failed: {}",
                String::from_utf8_lossy(&stage.stderr)
            )));
        }
        let digest = docker(&["commit", &staging.0])?;
        if !valid_digest(&digest) {
            return Err(invalid_archive("Docker commit returned invalid image ID"));
        }
        let mut owned_image = DerivedImage(digest);
        let derived = inspect_image(&owned_image.0)?;
        let config =
            |image: &Value, field: &str| image.pointer(&format!("/Config/{field}")).cloned();
        for field in ["Cmd", "Entrypoint", "Env", "User", "WorkingDir", "Volumes"] {
            if config(&base, field) != config(&derived, field) {
                return Err(invalid_archive(
                    "derived image changed carrier startup configuration",
                ));
            }
        }
        let base_layers = base
            .pointer("/RootFS/Layers")
            .and_then(Value::as_array)
            .ok_or_else(|| invalid_archive("carrier has no rootfs layers"))?;
        let derived_layers = derived
            .pointer("/RootFS/Layers")
            .and_then(Value::as_array)
            .ok_or_else(|| invalid_archive("derived image has no rootfs layers"))?;
        if derived_layers.len() != base_layers.len() + 1
            || &derived_layers[..base_layers.len()] != base_layers
        {
            return Err(invalid_archive(
                "derived image does not extend pinned carrier by one layer",
            ));
        }
        docker(&["rm", &staging.0])?;
        staging.0.clear();
        Ok(std::mem::take(&mut owned_image.0))
    }
}

fn unpack_archive(reader: impl Read, workspace: &Path) -> io::Result<()> {
    let mut archive = Archive::new(reader);
    let mut seen = HashSet::<PathBuf>::new();
    let mut total_size = 0_u64;
    for (index, entry) in archive.entries()?.enumerate() {
        if index >= MAX_ENTRIES {
            return Err(invalid_archive("archive contains too many entries"));
        }
        let mut entry = entry?;
        let path = entry.path()?.into_owned();
        if path.as_os_str().is_empty()
            || !path
                .components()
                .all(|part| matches!(part, Component::Normal(_) | Component::CurDir))
        {
            return Err(invalid_archive("archive entry escapes workspace"));
        }
        let kind = entry.header().entry_type();
        if !kind.is_file() && !kind.is_dir() {
            return Err(invalid_archive("archive contains a link or special file"));
        }
        let normalized: PathBuf = path
            .components()
            .filter_map(|part| match part {
                Component::Normal(name) => Some(name),
                _ => None,
            })
            .collect();
        if !normalized.as_os_str().is_empty() && !seen.insert(normalized) {
            return Err(invalid_archive("archive contains a duplicate path"));
        }
        total_size = total_size
            .checked_add(entry.size())
            .ok_or_else(|| invalid_archive("archive size overflow"))?;
        if total_size > MAX_EXTRACTED_BYTES {
            return Err(invalid_archive("archive exceeds extracted size limit"));
        }
        if !entry.unpack_in(workspace)? {
            return Err(invalid_archive("archive entry escapes workspace"));
        }
    }
    Ok(())
}

pub fn validate_base_image(image: &str) -> io::Result<()> {
    let (tag, digest) = image
        .split_once('@')
        .ok_or_else(|| invalid_archive("carrier image must have an immutable local image ID"))?;
    let versioned = tag
        .strip_prefix("substratum/castor-pi-carrier:one-shot-")
        .is_some_and(|version| {
            !version.is_empty()
                && !version.contains("..")
                && version
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
        });
    if tag != "substratum/castor-pi-carrier:v1" && !versioned {
        return Err(invalid_archive("carrier image tag is unsupported"));
    }
    if !valid_digest(digest) {
        return Err(invalid_archive("carrier image digest is invalid"));
    }
    Ok(())
}

pub fn valid_digest(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    })
}

fn invalid_archive(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod versioned_carrier_tests {
    use super::validate_base_image;

    #[test]
    fn versioned_carrier_pin_is_accepted_but_unpinned_or_malformed_is_not() {
        let id = format!("sha256:{}", "a".repeat(64));
        assert!(validate_base_image(&format!("substratum/castor-pi-carrier:v1@{id}")).is_ok());
        assert!(
            validate_base_image(&format!("substratum/castor-pi-carrier:one-shot-0.1.0@{id}"))
                .is_ok()
        );
        assert!(validate_base_image("substratum/castor-pi-carrier:one-shot-0.1.0").is_err());
        assert!(
            validate_base_image(&format!("substratum/castor-pi-carrier:bad/tag@{id}")).is_err()
        );
    }
}
