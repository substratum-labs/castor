//! Pinned, idempotent preparation of the one-shot Docker runtime.
use crate::one_shot::install::{ImagePin, InstalledRelease, PlatformImages};
use crate::one_shot::oci_layout::{export_layout, verify_layout};
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
    pub controller_ref: String,
    pub carrier_ref: String,
    pub verifier_ref: String,
    pub controller_id: String,
    pub carrier_id: String,
    pub verifier_id: String,
    pub carrier_tag: String,
}

#[derive(Debug, Clone)]
pub struct CarrierLayout {
    pub path: PathBuf,
    pub manifest_digest: String,
}

fn carrier_layout_path(receipt: &PreparedRuntime, state_root: &Path) -> io::Result<PathBuf> {
    if !valid_image_id(&receipt.carrier_id) {
        return Err(invalid("invalid prepared carrier ID"));
    }
    Ok(state_root.join("runtime").join(format!(
        "carrier-oci-{}-linux-{}-{}",
        receipt.release_version,
        receipt.engine_arch,
        &receipt.carrier_id[7..]
    )))
}

pub fn revalidate_carrier_layout(
    receipt: &PreparedRuntime,
    state_root: &Path,
) -> io::Result<CarrierLayout> {
    let path = carrier_layout_path(receipt, state_root)?;
    let manifest_digest = verify_layout(
        &path,
        &receipt.carrier_id,
        &receipt.engine_arch,
        Some(&receipt.source_revision),
    )?;
    Ok(CarrierLayout {
        path,
        manifest_digest,
    })
}

pub fn prepare_carrier_layout(
    receipt: &PreparedRuntime,
    state_root: &Path,
) -> io::Result<CarrierLayout> {
    if let Ok(layout) = revalidate_carrier_layout(receipt, state_root) {
        return Ok(layout);
    }
    let path = carrier_layout_path(receipt, state_root)?;
    match fs::symlink_metadata(&path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {
            fs::remove_dir_all(&path)?;
        }
        Ok(_) => return Err(invalid("prepared OCI layout has an unsafe file type")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => (),
        Err(error) => return Err(error),
    }
    let engine = DockerEngine;
    let before = engine
        .inspect(&receipt.carrier_tag)?
        .ok_or_else(|| invalid("prepared carrier tag is missing before OCI export"))?;
    if before.id != receipt.carrier_id {
        return Err(invalid("prepared carrier tag changed before OCI export"));
    }
    let manifest_digest = export_layout(
        &receipt.carrier_tag,
        &path,
        &receipt.carrier_id,
        &receipt.engine_arch,
        Some(&receipt.source_revision),
    )?;
    let after = engine
        .inspect(&receipt.carrier_tag)?
        .ok_or_else(|| invalid("prepared carrier tag disappeared after OCI export"))?;
    if after.id != receipt.carrier_id {
        return Err(invalid("prepared carrier tag changed during OCI export"));
    }
    Ok(CarrierLayout {
        path,
        manifest_digest,
    })
}

fn normalize_arch(value: &str) -> &str {
    match value {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    }
}

fn valid_image_id(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    })
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
    if !valid_image_id(&observed.id)
        || observed.os != "linux"
        || normalize_arch(&observed.arch) != arch
        || !digest_reference_matches(pin, &observed.repo_digests)
    {
        return Err(invalid(
            "prepared image digest, platform or local ID is invalid",
        ));
    }
    Ok(observed)
}

fn receipt_path(state_root: &Path, release: &InstalledRelease, arch: &str) -> PathBuf {
    state_root.join("runtime").join(format!(
        "{}-linux-{arch}.json",
        release.manifest.release_version
    ))
}

fn receipt_for(
    release: &InstalledRelease,
    pins: &PlatformImages,
    arch: &str,
    controller_id: String,
    carrier_id: String,
    verifier_id: String,
) -> PreparedRuntime {
    PreparedRuntime {
        release_version: release.manifest.release_version.clone(),
        source_revision: release.manifest.source_revision.clone(),
        engine_arch: arch.into(),
        controller_ref: pins.controller.reference.clone(),
        carrier_ref: pins.carrier.reference.clone(),
        verifier_ref: pins.verifier.reference.clone(),
        controller_id,
        carrier_id,
        verifier_id,
        carrier_tag: pins.carrier_tag.clone(),
    }
}

fn read_receipt(path: &Path) -> io::Result<PreparedRuntime> {
    let mut bytes = Vec::new();
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() > 4096 {
        return Err(invalid("invalid Castor runtime receipt"));
    }
    file.take(4097).read_to_end(&mut bytes)?;
    if bytes.len() > 4096 {
        return Err(invalid("invalid Castor runtime receipt"));
    }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}

fn verify_receipt_metadata(
    receipt: &PreparedRuntime,
    release: &InstalledRelease,
    pins: &PlatformImages,
    arch: &str,
) -> io::Result<()> {
    if receipt.release_version != release.manifest.release_version
        || receipt.source_revision != release.manifest.source_revision
        || receipt.engine_arch != arch
        || receipt.controller_ref != pins.controller.reference
        || receipt.carrier_ref != pins.carrier.reference
        || receipt.verifier_ref != pins.verifier.reference
        || receipt.carrier_tag != pins.carrier_tag
        || !valid_image_id(&receipt.controller_id)
        || !valid_image_id(&receipt.carrier_id)
        || !valid_image_id(&receipt.verifier_id)
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
    verify_receipt_metadata(&receipt, release, pins, &platform.arch)?;
    for (pin, expected_id) in [
        (&pins.controller, receipt.controller_id.as_str()),
        (&pins.carrier, receipt.carrier_id.as_str()),
        (&pins.verifier, receipt.verifier_id.as_str()),
    ] {
        if verify_image(engine, pin, &platform.arch)?.id != expected_id {
            return Err(invalid(
                "prepared image local ID changed since runtime prepare",
            ));
        }
    }
    let carrier = engine
        .inspect(&pins.carrier_tag)?
        .ok_or_else(|| invalid("prepared carrier tag is missing"))?;
    if carrier.id != receipt.carrier_id {
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
        if let Ok(receipt) = revalidate(release, engine, state_root) {
            return Ok(receipt);
        }
        // Explicit preparation may refresh a changed Engine-local representation;
        // all exact registry digests are reverified before replacing the receipt.
    }
    let mut observed = Vec::with_capacity(3);
    for pin in [&pins.controller, &pins.carrier, &pins.verifier] {
        if engine.inspect(&pin.reference)?.is_none() {
            engine.pull(&pin.reference)?;
        }
        observed.push(verify_image(engine, pin, &platform.arch)?);
    }
    let carrier_id = observed[1].id.clone();
    match engine.inspect(&pins.carrier_tag)? {
        Some(existing) if existing.id == carrier_id => (),
        Some(_) => {
            return Err(invalid(
                "Castor-owned carrier tag conflicts with release pin",
            ))
        }
        None => engine.tag(&carrier_id, &pins.carrier_tag)?,
    }
    let receipt = receipt_for(
        release,
        pins,
        &platform.arch,
        observed[0].id.clone(),
        carrier_id,
        observed[2].id.clone(),
    );
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
    let output =
        bounded_program("node", &["--version"], Duration::from_secs(5)).map_err(|error| {
            io::Error::new(
                error.kind(),
                format!("Node.js 22 or newer unavailable: {error}"),
            )
        })?;
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
