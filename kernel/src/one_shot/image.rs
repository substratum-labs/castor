use crate::one_shot::manifest::ValidatedSnapshot;
use flate2::read::GzDecoder;
use std::collections::HashSet;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use tar::Archive;

const MAX_ENTRIES: usize = 50_000;
const MAX_EXTRACTED_BYTES: u64 = 1024 * 1024 * 1024;

pub struct StagedSnapshot {
    root: tempfile::TempDir,
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
        let dockerfile = self.root.path().join("Dockerfile");
        fs::write(
            &dockerfile,
            "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nCOPY --chown=10001:10001 --chmod=0555 workspace_snapshot/ /workspace/\n",
        )?;
        let output = Command::new("docker")
            .arg("build")
            .arg("--quiet")
            .arg("--build-arg")
            .arg(format!("BASE_IMAGE={carrier_base_image}"))
            .arg("--file")
            .arg(&dockerfile)
            .arg(self.root.path())
            .output()?;
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

fn validate_base_image(image: &str) -> io::Result<()> {
    let Some(digest) = image.strip_prefix("substratum/castor-pi-carrier:v1@") else {
        return Err(invalid_archive(
            "carrier image must be pinned to the Pi v1 digest",
        ));
    };
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
