use crate::one_shot::manifest::ValidatedSnapshot;
use crate::one_shot::oci_layout::{export_layout, verify_layout};
use flate2::read::GzDecoder;
use std::collections::HashSet;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use tar::Archive;

const MAX_ENTRIES: usize = 50_000;
const MAX_EXTRACTED_BYTES: u64 = 1024 * 1024 * 1024;

pub struct StagedSnapshot {
    root: tempfile::TempDir,
}

struct ReadonlyWorkspaceGuard {
    original_modes: Vec<(PathBuf, u32)>,
}

impl ReadonlyWorkspaceGuard {
    fn new(workspace: &Path) -> io::Result<Self> {
        let mut guard = Self {
            original_modes: Vec::new(),
        };
        let mut pending = vec![workspace.to_owned()];
        while let Some(path) = pending.pop() {
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.is_dir() {
                for entry in fs::read_dir(&path)? {
                    pending.push(entry?.path());
                }
            } else if !metadata.is_file() {
                return Err(invalid_archive("snapshot contains a special file"));
            }
            guard
                .original_modes
                .push((path.clone(), metadata.permissions().mode() & 0o7777));
            fs::set_permissions(path, fs::Permissions::from_mode(0o555))?;
        }
        Ok(guard)
    }

    fn restore(&mut self) -> io::Result<()> {
        for (path, mode) in self.original_modes.iter().rev() {
            fs::set_permissions(path, fs::Permissions::from_mode(*mode))?;
        }
        self.original_modes.clear();
        Ok(())
    }
}

impl Drop for ReadonlyWorkspaceGuard {
    fn drop(&mut self) {
        let _ = self.restore();
    }
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
        let inspect = Command::new("docker")
            .args(["image", "inspect", "--format", "{{.Id}}", carrier_tag])
            .output()?;
        if !inspect.status.success() {
            return Err(io::Error::other(
                "pinned Pi carrier is not installed locally",
            ));
        }
        let local_id = String::from_utf8_lossy(&inspect.stdout).trim().to_owned();
        if local_id != expected_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "local Pi carrier digest differs from task manifest pin",
            ));
        }
        let inspect_arch = Command::new("docker")
            .args([
                "image",
                "inspect",
                "--format",
                "{{.Architecture}}",
                carrier_tag,
            ])
            .output()?;
        if !inspect_arch.status.success() {
            return Err(io::Error::other(
                "cannot inspect local Pi carrier architecture",
            ));
        }
        let arch = String::from_utf8_lossy(&inspect_arch.stdout)
            .trim()
            .to_owned();
        if arch != "amd64" && arch != "arm64" {
            return Err(io::Error::other(
                "unsupported local Pi carrier architecture",
            ));
        }
        let private_layout = tempfile::Builder::new()
            .prefix("castor-carrier-oci-")
            .tempdir()?;
        let (layout, manifest_digest) = match (
            std::env::var_os("CASTOR_CARRIER_OCI_LAYOUT"),
            std::env::var_os("CASTOR_CARRIER_OCI_DIGEST"),
        ) {
            (Some(path), Some(digest)) => {
                let path = PathBuf::from(path);
                if !path.is_absolute() {
                    return Err(io::Error::other("carrier OCI layout path is not absolute"));
                }
                let digest = digest
                    .into_string()
                    .map_err(|_| io::Error::other("invalid carrier OCI digest"))?;
                let actual = verify_layout(&path, expected_id, &arch, None)?;
                if actual != digest {
                    return Err(io::Error::other(
                        "carrier OCI layout digest differs from prepared runtime",
                    ));
                }
                (path, actual)
            }
            (None, None) => {
                let path = private_layout.path().join("carrier-oci");
                let digest = export_layout(carrier_tag, &path, expected_id, &arch, None)?;
                (path, digest)
            }
            _ => return Err(io::Error::other("incomplete carrier OCI layout input")),
        };
        let mut read_only = ReadonlyWorkspaceGuard::new(&self.workspace())?;
        let dockerfile = self.root.path().join("Dockerfile");
        fs::write(
            &dockerfile,
            "FROM castor_base\nCOPY --chown=10001:10001 workspace_snapshot/ /workspace/\n",
        )?;
        let build_result = Command::new("docker")
            .args(["buildx", "build"])
            .arg("--quiet")
            .arg("--load")
            .arg("--build-context")
            .arg(format!(
                "castor_base=oci-layout://{}@{manifest_digest}",
                layout.display()
            ))
            .arg("--file")
            .arg(&dockerfile)
            .arg(self.root.path())
            .output();
        read_only.restore()?;
        let output = build_result?;
        if !output.status.success() {
            return Err(io::Error::other(format!(
                "derived image build failed: {}",
                String::from_utf8_lossy(&output.stderr)
            )));
        }
        let digest = String::from_utf8_lossy(&output.stdout)
            .lines()
            .last()
            .unwrap_or_default()
            .trim()
            .to_owned();
        if !valid_digest(&digest) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "image builder returned no immutable sha256 digest",
            ));
        }
        let loaded = Command::new("docker")
            .args(["image", "inspect", "--format", "{{.Id}}", &digest])
            .output()?;
        if !loaded.status.success() || String::from_utf8_lossy(&loaded.stdout).trim() != digest {
            return Err(io::Error::other(
                "BuildKit result is not installed as the returned immutable image",
            ));
        }
        Ok(digest)
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
