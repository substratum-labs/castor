//! Bounded OCI image layout extraction for a verified Engine-local carrier.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use tar::{Archive, EntryType};

const MAX_FILES: usize = 2048;
const MAX_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const MAX_JSON: u64 = 1024 * 1024;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn digest_hex(value: &str) -> io::Result<&str> {
    let hex = value
        .strip_prefix("sha256:")
        .ok_or_else(|| invalid("OCI digest is not sha256"))?;
    if hex.len() != 64
        || !hex
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err(invalid("invalid OCI sha256 digest"));
    }
    Ok(hex)
}

fn blob(root: &Path, digest: &str) -> io::Result<PathBuf> {
    Ok(root.join("blobs/sha256").join(digest_hex(digest)?))
}

fn read_json(path: &Path) -> io::Result<Value> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.file_type().is_symlink() || meta.len() > MAX_JSON {
        return Err(invalid("invalid OCI JSON member"));
    }
    serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)
}

fn checked_blob(root: &Path, digest: &str, size: u64) -> io::Result<Value> {
    let path = blob(root, digest)?;
    let meta = fs::symlink_metadata(&path)?;
    if !meta.is_file() || meta.file_type().is_symlink() || meta.len() != size || size > MAX_JSON {
        return Err(invalid("OCI descriptor size or type differs"));
    }
    let bytes = fs::read(&path)?;
    if format!("{:x}", Sha256::digest(&bytes)) != digest_hex(digest)? {
        return Err(invalid("OCI descriptor hash differs"));
    }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}

fn descriptor(value: &Value) -> io::Result<(&str, u64)> {
    let digest = value
        .get("digest")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("missing OCI descriptor digest"))?;
    digest_hex(digest)?;
    let size = value
        .get("size")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid("missing OCI descriptor size"))?;
    Ok((digest, size))
}

pub fn verify_layout(
    root: &Path,
    expected_image_id: &str,
    expected_arch: &str,
    expected_revision: Option<&str>,
) -> io::Result<String> {
    digest_hex(expected_image_id)?;
    let meta = fs::symlink_metadata(root)?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return Err(invalid("OCI layout root is not a directory"));
    }
    for name in ["blobs", "blobs/sha256"] {
        let meta = fs::symlink_metadata(root.join(name))?;
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Err(invalid("OCI blob directory is not private and real"));
        }
    }
    let layout = read_json(&root.join("oci-layout"))?;
    if layout.get("imageLayoutVersion").and_then(Value::as_str) != Some("1.0.0") {
        return Err(invalid("unsupported OCI layout version"));
    }
    let index = read_json(&root.join("index.json"))?;
    let manifests = index
        .get("manifests")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("missing OCI index manifests"))?;
    if manifests.len() != 1 {
        return Err(invalid("OCI layout must select exactly one image"));
    }
    let (manifest_digest, manifest_size) = descriptor(&manifests[0])?;
    let manifest = checked_blob(root, manifest_digest, manifest_size)?;
    let (config_digest, config_size) = descriptor(
        manifest
            .get("config")
            .ok_or_else(|| invalid("missing OCI image config"))?,
    )?;
    let config = checked_blob(root, config_digest, config_size)?;
    if config.get("os").and_then(Value::as_str) != Some("linux")
        || config.get("architecture").and_then(Value::as_str) != Some(expected_arch)
    {
        return Err(invalid("OCI carrier platform differs from Engine"));
    }
    if expected_revision.is_some_and(|revision| {
        config
            .pointer("/config/Labels/org.opencontainers.image.revision")
            .and_then(Value::as_str)
            != Some(revision)
    }) {
        return Err(invalid("OCI carrier source revision differs from release"));
    }
    if expected_image_id != manifest_digest && expected_image_id != config_digest {
        return Err(invalid("OCI carrier differs from prepared Engine image ID"));
    }
    let layers = manifest
        .get("layers")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("missing OCI image layers"))?;
    for layer in layers {
        let (digest, size) = descriptor(layer)?;
        let path = blob(root, digest)?;
        let meta = fs::symlink_metadata(&path)?;
        if !meta.is_file() || meta.file_type().is_symlink() || meta.len() != size {
            return Err(invalid("OCI layer size or type differs"));
        }
    }
    // Hash every blob, including layers and otherwise-unused content, so a
    // modified local layout cannot be accepted based only on its index.
    let mut count = 0usize;
    let mut total = 0u64;
    for entry in fs::read_dir(root.join("blobs/sha256"))? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name
            .to_str()
            .ok_or_else(|| invalid("non-UTF8 OCI blob name"))?;
        digest_hex(&format!("sha256:{name}"))?;
        let meta = fs::symlink_metadata(entry.path())?;
        if !meta.is_file() || meta.file_type().is_symlink() {
            return Err(invalid("OCI blob is not a regular file"));
        }
        count += 1;
        total = total
            .checked_add(meta.len())
            .ok_or_else(|| invalid("OCI layout size overflow"))?;
        if count > MAX_FILES || total > MAX_BYTES {
            return Err(invalid("OCI layout exceeds bound"));
        }
        let mut file = File::open(entry.path())?;
        let mut hasher = Sha256::new();
        let mut buffer = [0u8; 64 * 1024];
        loop {
            let read = file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            hasher.update(&buffer[..read]);
        }
        if format!("{:x}", hasher.finalize()) != name {
            return Err(invalid("OCI blob hash differs from path"));
        }
    }
    Ok(manifest_digest.to_owned())
}

pub fn import_layout(
    archive: &Path,
    destination: &Path,
    expected_image_id: &str,
    expected_arch: &str,
    expected_revision: Option<&str>,
) -> io::Result<String> {
    if fs::symlink_metadata(destination).is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "OCI layout already exists",
        ));
    }
    let parent = destination
        .parent()
        .ok_or_else(|| invalid("OCI layout parent missing"))?;
    fs::create_dir_all(parent)?;
    let staging = tempfile::Builder::new()
        .prefix(".castor-oci-")
        .tempdir_in(parent)?;
    let mut archive = Archive::new(File::open(archive)?);
    let mut seen = HashSet::new();
    let mut total = 0u64;
    for (index, entry) in archive.entries()?.enumerate() {
        if index >= MAX_FILES + 8 {
            return Err(invalid("OCI archive has too many members"));
        }
        let mut entry = entry?;
        let path = entry.path()?.into_owned();
        if path.as_os_str().is_empty()
            || !path
                .components()
                .all(|component| matches!(component, Component::Normal(_)))
        {
            return Err(invalid("OCI archive path escapes layout"));
        }
        let name = path
            .to_str()
            .ok_or_else(|| invalid("non-UTF8 OCI archive path"))?;
        if !seen.insert(name.to_owned()) {
            return Err(invalid("duplicate OCI archive member"));
        }
        let kind = entry.header().entry_type();
        if kind == EntryType::Directory {
            let directory = name.trim_end_matches('/');
            if directory != "blobs" && directory != "blobs/sha256" {
                return Err(invalid("unexpected OCI directory"));
            }
            continue;
        }
        if kind != EntryType::Regular {
            return Err(invalid("OCI archive contains a link or special file"));
        }
        total = total
            .checked_add(entry.size())
            .ok_or_else(|| invalid("OCI archive size overflow"))?;
        if total > MAX_BYTES {
            return Err(invalid("OCI archive exceeds size bound"));
        }
        if name == "manifest.json" || name == "repositories" {
            continue;
        }
        let target = if name == "index.json" || name == "oci-layout" {
            staging.path().join(name)
        } else if let Some(hex) = name.strip_prefix("blobs/sha256/") {
            digest_hex(&format!("sha256:{hex}"))?;
            fs::create_dir_all(staging.path().join("blobs/sha256"))?;
            staging.path().join(name)
        } else {
            return Err(invalid("unexpected OCI archive member"));
        };
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&target)?;
        io::copy(&mut entry, &mut output)?;
        output.sync_all()?;
        fs::set_permissions(target, fs::Permissions::from_mode(0o444))?;
    }
    let manifest = verify_layout(
        staging.path(),
        expected_image_id,
        expected_arch,
        expected_revision,
    )?;
    File::open(staging.path())?.sync_all()?;
    fs::rename(staging.path(), destination)?;
    let _ = staging.keep();
    File::open(parent)?.sync_all()?;
    Ok(manifest)
}

pub fn export_layout(
    carrier_tag: &str,
    destination: &Path,
    expected_image_id: &str,
    expected_arch: &str,
    expected_revision: Option<&str>,
) -> io::Result<String> {
    let parent = destination
        .parent()
        .ok_or_else(|| invalid("OCI layout parent missing"))?;
    fs::create_dir_all(parent)?;
    let archive = tempfile::NamedTempFile::new_in(parent)?;
    let stdout = tempfile::tempfile()?;
    let stderr = tempfile::tempfile()?;
    let mut child = Command::new("docker")
        .args(["image", "save", "-o"])
        .arg(archive.path())
        .arg(carrier_tag)
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr.try_clone()?))
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(900);
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "Docker OCI export timed out",
            ));
        }
        thread::sleep(Duration::from_millis(50));
    };
    if !status.success() {
        return Err(invalid("Docker carrier OCI export failed"));
    }
    if archive.as_file().metadata()?.len() > MAX_BYTES {
        return Err(invalid("Docker carrier OCI export exceeds size bound"));
    }
    if archive_is_oci(archive.path())? {
        import_layout(
            archive.path(),
            destination,
            expected_image_id,
            expected_arch,
            expected_revision,
        )
    } else {
        import_docker_archive(
            archive.path(),
            destination,
            expected_image_id,
            expected_arch,
            expected_revision,
        )
    }
}

fn archive_is_oci(path: &Path) -> io::Result<bool> {
    let mut archive = Archive::new(File::open(path)?);
    let mut oci = false;
    for entry in archive.entries()? {
        let entry = entry?;
        if entry.path()?.as_ref() == Path::new("oci-layout") {
            oci = true;
            break;
        }
    }
    Ok(oci)
}

fn archive_name(path: &Path) -> io::Result<String> {
    if path.as_os_str().is_empty()
        || !path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err(invalid("Docker archive path escapes layout"));
    }
    path.to_str()
        .map(str::to_owned)
        .ok_or_else(|| invalid("non-UTF8 Docker archive path"))
}

fn write_blob(root: &Path, reader: &mut impl Read) -> io::Result<(String, u64)> {
    let mut staged = tempfile::NamedTempFile::new_in(root)?;
    let mut hasher = Sha256::new();
    let mut size = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let n = reader.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        size = size
            .checked_add(n as u64)
            .ok_or_else(|| invalid("Docker archive size overflow"))?;
        if size > MAX_BYTES {
            return Err(invalid("Docker archive blob exceeds size bound"));
        }
        hasher.update(&buffer[..n]);
        std::io::Write::write_all(staged.as_file_mut(), &buffer[..n])?;
    }
    staged.as_file().sync_all()?;
    let digest = format!("sha256:{:x}", hasher.finalize());
    let target = root.join(digest_hex(&digest)?);
    staged.persist(&target).map_err(|error| error.error)?;
    fs::set_permissions(&target, fs::Permissions::from_mode(0o444))?;
    Ok((digest, size))
}

/// Convert the classic `docker image save` archive to a content-addressed OCI
/// layout. This is needed on Engines without the containerd image store.
pub fn import_docker_archive(
    archive_path: &Path,
    destination: &Path,
    expected_image_id: &str,
    expected_arch: &str,
    expected_revision: Option<&str>,
) -> io::Result<String> {
    digest_hex(expected_image_id)?;
    if fs::symlink_metadata(destination).is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "OCI layout already exists",
        ));
    }
    let parent = destination
        .parent()
        .ok_or_else(|| invalid("OCI layout parent missing"))?;
    fs::create_dir_all(parent)?;
    // The metadata pass permits at most one saved image and never extracts
    // archive paths. The second pass copies only its config and layer tars.
    let mut archive = Archive::new(File::open(archive_path)?);
    let mut manifest_bytes = None;
    for (index, entry) in archive.entries()?.enumerate() {
        if index >= MAX_FILES + 8 {
            return Err(invalid("Docker archive has too many members"));
        }
        let mut entry = entry?;
        let name = archive_name(&entry.path()?)?;
        if name == "manifest.json" {
            if manifest_bytes.is_some()
                || !entry.header().entry_type().is_file()
                || entry.size() > MAX_JSON
            {
                return Err(invalid("invalid Docker archive manifest"));
            }
            let mut bytes = Vec::new();
            entry.read_to_end(&mut bytes)?;
            manifest_bytes = Some(bytes);
        }
    }
    let manifests: Value = serde_json::from_slice(
        &manifest_bytes.ok_or_else(|| invalid("missing Docker archive manifest"))?,
    )
    .map_err(io::Error::other)?;
    let manifests = manifests
        .as_array()
        .ok_or_else(|| invalid("invalid Docker archive manifest"))?;
    if manifests.len() != 1 {
        return Err(invalid("Docker archive must contain exactly one image"));
    }
    let image = &manifests[0];
    let config_name = archive_name(Path::new(
        image
            .get("Config")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing Docker archive config"))?,
    ))?;
    let config_hex = config_name
        .strip_suffix(".json")
        .ok_or_else(|| invalid("invalid Docker config path"))?;
    if config_hex.contains('/') || format!("sha256:{config_hex}") != expected_image_id {
        return Err(invalid(
            "Docker archive config differs from Engine image ID",
        ));
    }
    let layer_names: Vec<String> = image
        .get("Layers")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("missing Docker archive layers"))?
        .iter()
        .map(|layer| {
            let name = archive_name(Path::new(
                layer
                    .as_str()
                    .ok_or_else(|| invalid("invalid Docker layer path"))?,
            ))?;
            if !name.ends_with("/layer.tar") {
                return Err(invalid("invalid Docker layer path"));
            }
            Ok(name)
        })
        .collect::<io::Result<_>>()?;
    let selected: HashSet<_> = std::iter::once(config_name.clone())
        .chain(layer_names.iter().cloned())
        .collect();
    if selected.len() != layer_names.len() + 1 {
        return Err(invalid("duplicate Docker archive layer"));
    }
    let staging = tempfile::Builder::new()
        .prefix(".castor-oci-")
        .tempdir_in(parent)?;
    let blobs = staging.path().join("blobs/sha256");
    fs::create_dir_all(&blobs)?;
    let mut found = HashSet::new();
    let mut copied = std::collections::HashMap::new();
    let mut total = 0u64;
    let mut archive = Archive::new(File::open(archive_path)?);
    for (index, entry) in archive.entries()?.enumerate() {
        if index >= MAX_FILES + 8 {
            return Err(invalid("Docker archive has too many members"));
        }
        let mut entry = entry?;
        let name = archive_name(&entry.path()?)?;
        let kind = entry.header().entry_type();
        if !kind.is_file() && !kind.is_dir() {
            return Err(invalid("Docker archive contains a link or special file"));
        }
        total = total
            .checked_add(entry.size())
            .ok_or_else(|| invalid("Docker archive size overflow"))?;
        if total > MAX_BYTES {
            return Err(invalid("Docker archive exceeds size bound"));
        }
        if !found.insert(name.clone()) {
            return Err(invalid("duplicate Docker archive member"));
        }
        if selected.contains(&name) {
            if !kind.is_file() {
                return Err(invalid("selected Docker archive member is not a file"));
            }
            copied.insert(name, write_blob(&blobs, &mut entry)?);
        }
    }
    if copied.len() != selected.len() {
        return Err(invalid("Docker archive is missing config or layer"));
    }
    let (config_digest, config_size) = copied
        .get(&config_name)
        .ok_or_else(|| invalid("missing Docker config"))?;
    if config_digest != expected_image_id || *config_size > MAX_JSON {
        return Err(invalid("Docker config hash or size differs"));
    }
    let config: Value = read_json(&blob(staging.path(), config_digest)?)?;
    let diff_ids = config
        .pointer("/rootfs/diff_ids")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("Docker config has no layer diff IDs"))?;
    if diff_ids.len() != layer_names.len() {
        return Err(invalid("Docker layer count differs from config"));
    }
    let mut layers = Vec::new();
    for (name, diff_id) in layer_names.iter().zip(diff_ids) {
        let (digest, size) = copied
            .get(name)
            .ok_or_else(|| invalid("missing Docker layer"))?;
        if diff_id.as_str() != Some(digest) {
            return Err(invalid("Docker layer differs from config diff ID"));
        }
        layers.push(json!({"mediaType":"application/vnd.oci.image.layer.v1.tar", "digest":digest,"size":size}));
    }
    let manifest = serde_json::to_vec(&json!({
        "schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",
        "config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":config_digest,"size":config_size},
        "layers":layers
    })).map_err(io::Error::other)?;
    let (manifest_digest, manifest_size) = write_blob(&blobs, &mut manifest.as_slice())?;
    let index = json!({"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json",
        "manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json",
            "digest":manifest_digest,"size":manifest_size,
            "platform":{"os":"linux","architecture":expected_arch}}]});
    fs::write(
        staging.path().join("oci-layout"),
        b"{\"imageLayoutVersion\":\"1.0.0\"}",
    )?;
    fs::write(
        staging.path().join("index.json"),
        serde_json::to_vec(&index).map_err(io::Error::other)?,
    )?;
    let actual = verify_layout(
        staging.path(),
        expected_image_id,
        expected_arch,
        expected_revision,
    )?;
    File::open(staging.path())?.sync_all()?;
    fs::rename(staging.path(), destination)?;
    let _ = staging.keep();
    File::open(parent)?.sync_all()?;
    Ok(actual)
}
