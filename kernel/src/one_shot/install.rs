//! Strict, source-checkout-independent release bundle identity.
use serde::de::{MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

const MAX_MANIFEST_BYTES: u64 = 64 * 1024;
const MAX_BINARY_BYTES: u64 = 128 * 1024 * 1024;
const MAX_SCRIPT_BYTES: u64 = 4 * 1024 * 1024;

fn invalid(reason: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, reason)
}

fn valid_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn valid_reference(value: &str) -> bool {
    let Some((repo, digest)) = value.rsplit_once("@sha256:") else {
        return false;
    };
    repo.bytes()
        .next()
        .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && repo.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"./:_-".contains(&byte)
        })
        && !repo.contains("..")
        && valid_hex(digest, 64)
}

fn host_os() -> &'static str {
    if cfg!(target_os = "macos") {
        "darwin"
    } else {
        "linux"
    }
}

fn host_arch() -> &'static str {
    if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "amd64"
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostAssets {
    pub os: String,
    pub arch: String,
    pub castor_sha256: String,
    pub model_pin_sha256: String,
    pub adapter_sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImagePin {
    pub reference: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlatformImages {
    pub controller: ImagePin,
    pub carrier: ImagePin,
    pub verifier: ImagePin,
    pub carrier_tag: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseManifest {
    pub schema_version: u32,
    pub release_version: String,
    pub source_revision: String,
    pub host: HostAssets,
    pub images: BTreeMap<String, PlatformImages>,
}

impl ReleaseManifest {
    fn validate(&self) -> io::Result<()> {
        if self.schema_version != 1
            || self.release_version.is_empty()
            || !self
                .release_version
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'.' || byte == b'-')
            || !valid_hex(&self.source_revision, 40)
            || self.host.os != host_os()
            || self.host.arch != host_arch()
            || !valid_hex(&self.host.castor_sha256, 64)
            || !valid_hex(&self.host.model_pin_sha256, 64)
            || !valid_hex(&self.host.adapter_sha256, 64)
            || self.images.len() != 2
            || !self.images.contains_key("linux/amd64")
            || !self.images.contains_key("linux/arm64")
        {
            return Err(invalid("invalid or unsupported Castor release manifest"));
        }
        for pins in self.images.values() {
            if pins.carrier_tag
                != format!(
                    "substratum/castor-pi-carrier:one-shot-{}",
                    self.release_version
                )
                || [&pins.controller, &pins.carrier, &pins.verifier]
                    .iter()
                    .any(|pin| !valid_reference(&pin.reference))
            {
                return Err(invalid("invalid Castor release image pin"));
            }
        }
        Ok(())
    }
}

/// Intermediate JSON representation rejecting duplicate keys at every depth.
struct UniqueJson(Value);

impl<'de> Deserialize<'de> for UniqueJson {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct UniqueVisitor;
        impl<'de> Visitor<'de> for UniqueVisitor {
            type Value = UniqueJson;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("JSON without duplicate object keys")
            }
            fn visit_bool<E: serde::de::Error>(self, value: bool) -> Result<Self::Value, E> {
                Ok(UniqueJson(Value::Bool(value)))
            }
            fn visit_i64<E: serde::de::Error>(self, value: i64) -> Result<Self::Value, E> {
                Ok(UniqueJson(value.into()))
            }
            fn visit_u64<E: serde::de::Error>(self, value: u64) -> Result<Self::Value, E> {
                Ok(UniqueJson(value.into()))
            }
            fn visit_f64<E: serde::de::Error>(self, value: f64) -> Result<Self::Value, E> {
                serde_json::Number::from_f64(value)
                    .map(|number| UniqueJson(Value::Number(number)))
                    .ok_or_else(|| E::custom("non-finite JSON number"))
            }
            fn visit_str<E: serde::de::Error>(self, value: &str) -> Result<Self::Value, E> {
                Ok(UniqueJson(Value::String(value.to_owned())))
            }
            fn visit_string<E: serde::de::Error>(self, value: String) -> Result<Self::Value, E> {
                Ok(UniqueJson(Value::String(value)))
            }
            fn visit_none<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(UniqueJson(Value::Null))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(UniqueJson(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(item) = seq.next_element::<UniqueJson>()? {
                    values.push(item.0);
                }
                Ok(UniqueJson(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut keys = HashSet::new();
                let mut values = serde_json::Map::new();
                while let Some((key, item)) = map.next_entry::<String, UniqueJson>()? {
                    if !keys.insert(key.clone()) {
                        return Err(serde::de::Error::custom("duplicate JSON key"));
                    }
                    values.insert(key, item.0);
                }
                Ok(UniqueJson(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(UniqueVisitor)
    }
}

fn open_regular(path: &Path, max_bytes: u64) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > max_bytes {
        return Err(invalid("release asset is not a bounded regular file"));
    }
    Ok(file)
}

fn sha256_file(path: &Path, max_bytes: u64) -> io::Result<String> {
    let mut file = open_regular(path, max_bytes)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 8192];
    let mut total = 0u64;
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        total += count as u64;
        if total > max_bytes {
            return Err(invalid("release asset exceeds size cap"));
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

#[derive(Debug, Clone, Copy)]
pub enum HostScript {
    ModelPin,
    OllamaAdapter,
}

pub struct InstalledRelease {
    pub manifest: ReleaseManifest,
    model_pin_script: PathBuf,
    adapter_script: PathBuf,
}

impl InstalledRelease {
    pub fn load_current() -> io::Result<Self> {
        Self::load_at(&std::env::current_exe()?)
    }

    pub fn load_at(executable: &Path) -> io::Result<Self> {
        let binary = fs::canonicalize(executable)?;
        let bin_dir = binary
            .parent()
            .ok_or_else(|| invalid("release binary has no parent"))?;
        if bin_dir.file_name().is_none_or(|name| name != "bin")
            || binary.file_name().is_none_or(|name| name != "castor")
        {
            return Err(invalid("castor executable is outside a release bundle"));
        }
        let root = bin_dir
            .parent()
            .ok_or_else(|| invalid("release root missing"))?;
        let manifest_path = root.join("share/castor/release.json");
        let mut manifest_bytes = Vec::new();
        open_regular(&manifest_path, MAX_MANIFEST_BYTES)?.read_to_end(&mut manifest_bytes)?;
        let unique: UniqueJson =
            serde_json::from_slice(&manifest_bytes).map_err(io::Error::other)?;
        let manifest: ReleaseManifest =
            serde_json::from_value(unique.0).map_err(io::Error::other)?;
        manifest.validate()?;
        let model_pin_script = root.join("libexec/castor/model_pin.mjs");
        let adapter_script = root.join("libexec/castor/ollama_model_adapter.mjs");
        for (path, expected, limit) in [
            (
                binary.as_path(),
                manifest.host.castor_sha256.as_str(),
                MAX_BINARY_BYTES,
            ),
            (
                model_pin_script.as_path(),
                manifest.host.model_pin_sha256.as_str(),
                MAX_SCRIPT_BYTES,
            ),
            (
                adapter_script.as_path(),
                manifest.host.adapter_sha256.as_str(),
                MAX_SCRIPT_BYTES,
            ),
        ] {
            if sha256_file(path, limit)? != expected {
                return Err(invalid("Castor release asset hash mismatch"));
            }
        }
        Ok(Self {
            manifest,
            model_pin_script,
            adapter_script,
        })
    }

    pub fn script(&self, name: HostScript) -> &Path {
        match name {
            HostScript::ModelPin => &self.model_pin_script,
            HostScript::OllamaAdapter => &self.adapter_script,
        }
    }

    pub fn pins(&self, engine_arch: &str) -> io::Result<&PlatformImages> {
        self.manifest
            .images
            .get(&format!("linux/{engine_arch}"))
            .ok_or_else(|| invalid("unsupported Docker Engine architecture"))
    }
}
