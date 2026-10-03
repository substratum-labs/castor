use castor_kernel::one_shot::install::{HostScript, InstalledRelease};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
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

fn pin(name: &str) -> Value {
    json!({
        "reference": format!("ghcr.io/substratum-labs/{name}@sha256:{}", "a".repeat(64)),
        "image_id": format!("sha256:{}", "b".repeat(64))
    })
}

fn fixture() -> (tempfile::TempDir, PathBuf) {
    let root = tempfile::tempdir().unwrap();
    let bin = root.path().join("bin");
    let scripts = root.path().join("libexec/castor");
    let share = root.path().join("share/castor");
    let link = root.path().join("link");
    for dir in [&bin, &scripts, &share, &link] {
        fs::create_dir_all(dir).unwrap();
    }
    fs::write(bin.join("castor"), b"fake executable").unwrap();
    fs::write(scripts.join("model_pin.mjs"), b"model pin").unwrap();
    fs::write(scripts.join("ollama_model_adapter.mjs"), b"adapter").unwrap();
    symlink(bin.join("castor"), link.join("castor")).unwrap();
    let platform = json!({
        "controller": pin("castor-controller"),
        "carrier": pin("castor-pi-carrier"),
        "verifier": pin("castor-verifier"),
        "carrier_tag": "substratum/castor-pi-carrier:one-shot-0.1.0"
    });
    let manifest = json!({
        "schema_version": 1,
        "release_version": "0.1.0",
        "source_revision": "c".repeat(40),
        "host": {
            "os": host_os(), "arch": host_arch(),
            "castor_sha256": digest(b"fake executable"),
            "model_pin_sha256": digest(b"model pin"),
            "adapter_sha256": digest(b"adapter")
        },
        "images": {"linux/amd64": platform, "linux/arm64": platform}
    });
    fs::write(
        share.join("release.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();
    let exe = link.join("castor");
    (root, exe)
}

fn release_file(root: &Path) -> PathBuf {
    root.join("share/castor/release.json")
}

#[test]
fn loads_from_path_symlink_and_rejects_changed_script() {
    let (root, exe) = fixture();
    let installed = InstalledRelease::load_at(&exe).unwrap();
    assert_eq!(installed.manifest.release_version, "0.1.0");
    assert!(installed
        .script(HostScript::ModelPin)
        .ends_with("libexec/castor/model_pin.mjs"));
    assert_eq!(
        installed.pins("amd64").unwrap().carrier_tag,
        "substratum/castor-pi-carrier:one-shot-0.1.0"
    );
    fs::write(
        root.path().join("libexec/castor/ollama_model_adapter.mjs"),
        b"changed",
    )
    .unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
}

#[test]
fn rejects_missing_and_symlinked_assets() {
    let (root, exe) = fixture();
    let path = root.path().join("libexec/castor/model_pin.mjs");
    fs::remove_file(&path).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
    fs::write(&path, b"model pin").unwrap();
    let moved = root.path().join("elsewhere.mjs");
    fs::rename(&path, &moved).unwrap();
    symlink(&moved, &path).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
}

#[test]
fn rejects_host_mismatch_and_unknown_fields() {
    let (root, exe) = fixture();
    let path = release_file(root.path());
    let mut body: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    body["host"]["arch"] = json!("wrong");
    fs::write(&path, serde_json::to_vec(&body).unwrap()).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
    body["host"]["arch"] = json!(host_arch());
    body["surprise"] = json!(true);
    fs::write(&path, serde_json::to_vec(&body).unwrap()).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
}

#[test]
fn rejects_duplicate_json_keys() {
    let (root, exe) = fixture();
    let path = release_file(root.path());
    let body = String::from_utf8(fs::read(&path).unwrap()).unwrap();
    let duplicate = body.replacen(
        "\"schema_version\":1",
        "\"schema_version\":1,\"schema_version\":1",
        1,
    );
    assert_ne!(duplicate, body);
    fs::write(&path, duplicate).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
}

#[test]
fn rejects_option_shaped_image_reference() {
    let (root, exe) = fixture();
    let path = release_file(root.path());
    let mut body: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    body["images"]["linux/amd64"]["controller"]["reference"] =
        json!(format!("-unexpected@sha256:{}", "a".repeat(64)));
    fs::write(&path, serde_json::to_vec(&body).unwrap()).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
}

#[test]
fn rejects_nested_duplicate_pin_key() {
    let (root, exe) = fixture();
    let path = release_file(root.path());
    let body = String::from_utf8(fs::read(&path).unwrap()).unwrap();
    let old = format!("\"image_id\":\"sha256:{}\"", "b".repeat(64));
    let replacement = format!("{old},{old}");
    let duplicate = body.replacen(&old, &replacement, 1);
    assert_ne!(duplicate, body);
    fs::write(&path, duplicate).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
}
