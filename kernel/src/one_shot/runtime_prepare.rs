//! Pinned, idempotent preparation of the one-shot Docker runtime.
use crate::one_shot::install::{ImagePin, InstalledRelease, PlatformImages};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const COMMAND_TIMEOUT: Duration = Duration::from_secs(900);
const COMMAND_OUTPUT_LIMIT: u64 = 2 * 1024 * 1024;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnginePlatform {
    pub os: String,
    pub arch: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageObservation {
    pub id: String,
    pub repo_digests: Vec<String>,
    pub os: String,
    pub arch: String,
}

pub trait EngineOps {
    fn platform(&self) -> io::Result<EnginePlatform>;
    fn pull(&self, reference: &str) -> io::Result<()>;
    fn inspect(&self, reference: &str) -> io::Result<Option<ImageObservation>>;
    fn tag(&self, id: &str, tag: &str) -> io::Result<()>;
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PreparedRuntime {
    pub release_version: String,
    pub source_revision: String,
    pub engine_arch: String,
    pub controller_id: String,
    pub carrier_id: String,
    pub verifier_id: String,
    pub carrier_tag: String,
}

fn normalize_arch(value: &str) -> &str {
    match value {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    }
}

fn checked_platform(
    release: &InstalledRelease,
    engine: &impl EngineOps,
) -> io::Result<EnginePlatform> {
    let mut platform = engine.platform()?;
    platform.arch = normalize_arch(&platform.arch).to_owned();
    if platform.os != "linux" {
        return Err(invalid("Castor requires a local Linux Docker Engine"));
    }
    release.pins(&platform.arch)?;
    Ok(platform)
}

fn digest_reference_matches(pin: &ImagePin, observed: &[String]) -> bool {
    if observed.contains(&pin.reference) {
        return true;
    }
    let Some(library) = pin.reference.strip_prefix("docker.io/library/") else {
        return false;
    };
    let Some((name_and_tag, digest)) = library.split_once('@') else {
        return false;
    };
    let short_name = name_and_tag.split(':').next().unwrap_or_default();
    observed
        .iter()
        .any(|value| value == &format!("{short_name}@{digest}"))
}

fn verify_image(
    engine: &impl EngineOps,
    pin: &ImagePin,
    arch: &str,
) -> io::Result<ImageObservation> {
    let observed = engine
        .inspect(&pin.reference)?
        .ok_or_else(|| invalid("prepared image is missing"))?;
    if observed.id != pin.image_id
        || observed.os != "linux"
        || normalize_arch(&observed.arch) != arch
        || !digest_reference_matches(pin, &observed.repo_digests)
    {
        return Err(invalid("prepared image identity differs from release pin"));
    }
    Ok(observed)
}

fn receipt_path(state_root: &Path, release: &InstalledRelease, arch: &str) -> PathBuf {
    state_root.join("runtime").join(format!(
        "{}-linux-{arch}.json",
        release.manifest.release_version
    ))
}

fn receipt_for(release: &InstalledRelease, pins: &PlatformImages, arch: &str) -> PreparedRuntime {
    PreparedRuntime {
        release_version: release.manifest.release_version.clone(),
        source_revision: release.manifest.source_revision.clone(),
        engine_arch: arch.into(),
        controller_id: pins.controller.image_id.clone(),
        carrier_id: pins.carrier.image_id.clone(),
        verifier_id: pins.verifier.image_id.clone(),
        carrier_tag: pins.carrier_tag.clone(),
    }
}

fn read_receipt(path: &Path) -> io::Result<PreparedRuntime> {
    let mut bytes = Vec::new();
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() > 4096 {
        return Err(invalid("invalid Castor runtime receipt"));
    }
    file.read_to_end(&mut bytes)?;
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}

fn verify_receipt(
    receipt: &PreparedRuntime,
    release: &InstalledRelease,
    pins: &PlatformImages,
    arch: &str,
) -> io::Result<()> {
    let expected = receipt_for(release, pins, arch);
    if receipt.release_version != expected.release_version
        || receipt.source_revision != expected.source_revision
        || receipt.engine_arch != expected.engine_arch
        || receipt.controller_id != expected.controller_id
        || receipt.carrier_id != expected.carrier_id
        || receipt.verifier_id != expected.verifier_id
        || receipt.carrier_tag != expected.carrier_tag
    {
        return Err(invalid(
            "Castor runtime receipt differs from installed release",
        ));
    }
    Ok(())
}

pub fn revalidate(
    release: &InstalledRelease,
    engine: &impl EngineOps,
    state_root: &Path,
) -> io::Result<PreparedRuntime> {
    let platform = checked_platform(release, engine)?;
    let pins = release.pins(&platform.arch)?;
    let receipt = read_receipt(&receipt_path(state_root, release, &platform.arch))?;
    verify_receipt(&receipt, release, pins, &platform.arch)?;
    for pin in [&pins.controller, &pins.carrier, &pins.verifier] {
        verify_image(engine, pin, &platform.arch)?;
    }
    let carrier = engine
        .inspect(&pins.carrier_tag)?
        .ok_or_else(|| invalid("prepared carrier tag is missing"))?;
    if carrier.id != pins.carrier.image_id {
        return Err(invalid("prepared carrier tag changed"));
    }
    Ok(receipt)
}

pub fn prepare(
    release: &InstalledRelease,
    engine: &impl EngineOps,
    state_root: &Path,
) -> io::Result<PreparedRuntime> {
    let platform = checked_platform(release, engine)?;
    let pins = release.pins(&platform.arch)?;
    let path = receipt_path(state_root, release, &platform.arch);
    if path.exists() {
        return revalidate(release, engine, state_root);
    }
    for pin in [&pins.controller, &pins.carrier, &pins.verifier] {
        if engine.inspect(&pin.reference)?.is_none() {
            engine.pull(&pin.reference)?;
        }
        verify_image(engine, pin, &platform.arch)?;
    }
    match engine.inspect(&pins.carrier_tag)? {
        Some(observed) if observed.id == pins.carrier.image_id => (),
        Some(_) => {
            return Err(invalid(
                "Castor-owned carrier tag conflicts with release pin",
            ))
        }
        None => engine.tag(&pins.carrier.image_id, &pins.carrier_tag)?,
    }
    let receipt = receipt_for(release, pins, &platform.arch);
    let parent = path
        .parent()
        .ok_or_else(|| invalid("runtime state parent missing"))?;
    fs::create_dir_all(parent)?;
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    let mut staging = tempfile::NamedTempFile::new_in(parent)?;
    staging
        .as_file_mut()
        .write_all(&serde_json::to_vec(&receipt).map_err(io::Error::other)?)?;
    staging.as_file_mut().sync_all()?;
    staging.persist(&path).map_err(|error| error.error)?;
    File::open(parent)?.sync_all()?;
    Ok(receipt)
}

fn read_capped(mut file: File) -> io::Result<Vec<u8>> {
    file.seek(SeekFrom::Start(0))?;
    let mut bytes = Vec::new();
    file.take(COMMAND_OUTPUT_LIMIT + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > COMMAND_OUTPUT_LIMIT {
        return Err(invalid("Docker command output exceeds cap"));
    }
    Ok(bytes)
}

fn bounded_program(
    program: &str,
    args: &[&str],
    timeout: Duration,
) -> io::Result<std::process::Output> {
    let stdout = tempfile::tempfile()?;
    let stderr = tempfile::tempfile()?;
    let mut child = Command::new(program)
        .args(args)
        .stdout(Stdio::from(stdout.try_clone()?))
        .stderr(Stdio::from(stderr.try_clone()?))
        .spawn()?;
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("{program} command timed out"),
            ));
        }
        thread::sleep(Duration::from_millis(25));
    };
    Ok(std::process::Output {
        status,
        stdout: read_capped(stdout)?,
        stderr: read_capped(stderr)?,
    })
}

pub fn check_node_major() -> io::Result<()> {
    let output = bounded_program("node", &["--version"], Duration::from_secs(5))?;
    let version = std::str::from_utf8(&output.stdout)
        .map_err(|_| invalid("invalid Node.js version output"))?;
    let major: u32 = version
        .trim()
        .strip_prefix('v')
        .and_then(|v| v.split('.').next())
        .ok_or_else(|| invalid("invalid Node.js version output"))?
        .parse()
        .map_err(|_| invalid("invalid Node.js version output"))?;
    if !output.status.success() || major < 22 {
        return Err(invalid("Castor requires Node.js 22 or newer"));
    }
    Ok(())
}

#[derive(Default)]
pub struct DockerEngine;

impl DockerEngine {
    fn call(&self, args: &[&str], timeout: Duration) -> io::Result<std::process::Output> {
        bounded_program("docker", args, timeout)
    }
}

impl EngineOps for DockerEngine {
    fn platform(&self) -> io::Result<EnginePlatform> {
        let output = self.call(
            &["info", "--format", "{{.OSType}}/{{.Architecture}}"],
            Duration::from_secs(15),
        )?;
        if !output.status.success() {
            return Err(invalid("Docker Engine is unavailable"));
        }
        let rendered = std::str::from_utf8(&output.stdout)
            .map_err(|_| invalid("invalid Docker platform output"))?;
        let (os, arch) = rendered
            .trim()
            .split_once('/')
            .ok_or_else(|| invalid("invalid Docker platform output"))?;
        Ok(EnginePlatform {
            os: os.into(),
            arch: normalize_arch(arch).into(),
        })
    }
    fn pull(&self, reference: &str) -> io::Result<()> {
        let output = self.call(&["pull", "--quiet", reference], COMMAND_TIMEOUT)?;
        if !output.status.success() {
            return Err(invalid("pinned Docker image pull failed"));
        }
        Ok(())
    }
    fn inspect(&self, reference: &str) -> io::Result<Option<ImageObservation>> {
        let output = self.call(
            &["image", "inspect", "--format", "{{json .}}", reference],
            Duration::from_secs(15),
        )?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            if stderr.contains("No such image") || stderr.contains("No such object") {
                return Ok(None);
            }
            return Err(invalid("Docker image inspection failed"));
        }
        let value: Value = serde_json::from_slice(&output.stdout).map_err(io::Error::other)?;
        let id = value
            .get("Id")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("Docker image ID missing"))?;
        let os = value
            .get("Os")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("Docker image OS missing"))?;
        let arch = value
            .get("Architecture")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("Docker image architecture missing"))?;
        let repo_digests = value
            .get("RepoDigests")
            .and_then(Value::as_array)
            .ok_or_else(|| invalid("Docker image RepoDigests missing"))?
            .iter()
            .map(|item| {
                item.as_str()
                    .map(ToOwned::to_owned)
                    .ok_or_else(|| invalid("invalid Docker RepoDigest"))
            })
            .collect::<io::Result<Vec<_>>>()?;
        Ok(Some(ImageObservation {
            id: id.into(),
            repo_digests,
            os: os.into(),
            arch: normalize_arch(arch).into(),
        }))
    }
    fn tag(&self, id: &str, tag: &str) -> io::Result<()> {
        let output = self.call(&["tag", id, tag], Duration::from_secs(15))?;
        if !output.status.success() {
            return Err(invalid("Castor carrier tag creation failed"));
        }
        Ok(())
    }
}
