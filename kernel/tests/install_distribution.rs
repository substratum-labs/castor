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
        "reference": format!("ghcr.io/substratum-labs/{name}@sha256:{}", "a".repeat(64))
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
    let old = format!(
        "\"reference\":\"ghcr.io/substratum-labs/castor-controller@sha256:{}\"",
        "a".repeat(64)
    );
    let replacement = format!("{old},{old}");
    let duplicate = body.replacen(&old, &replacement, 1);
    assert_ne!(duplicate, body);
    fs::write(&path, duplicate).unwrap();
    assert!(InstalledRelease::load_at(&exe).is_err());
}

#[test]
fn installed_run_rejects_missing_prepared_runtime_before_model_or_controller_build() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;
    let (root, exe) = fixture();
    fs::copy(env!("CARGO_BIN_EXE_castor"), root.path().join("bin/castor")).unwrap();
    fs::set_permissions(
        root.path().join("bin/castor"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let manifest_path = release_file(root.path());
    let mut manifest: Value = serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    manifest["host"]["castor_sha256"] =
        json!(digest(&fs::read(root.path().join("bin/castor")).unwrap()));
    fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let project = root.path().join("project");
    fs::create_dir(&project).unwrap();
    fs::write(project.join("hello.txt"), b"hello\n").unwrap();
    for args in [
        vec!["init", "-q"],
        vec!["add", "hello.txt"],
        vec![
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "init",
        ],
    ] {
        assert!(Command::new("git")
            .args(args)
            .current_dir(&project)
            .status()
            .unwrap()
            .success());
    }
    let spec = root.path().join("spec.json");
    fs::write(&spec, br#"{"schema_version":1,"task_prompt":"Fix hello","verification_command":["cat","hello.txt"]}"#).unwrap();
    let fake_bin = root.path().join("fake-bin");
    fs::create_dir(&fake_bin).unwrap();
    let docker_log = root.path().join("docker.log");
    let node_log = root.path().join("node.log");
    let docker = fake_bin.join("docker");
    fs::write(&docker, b"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CASTOR_FAKE_DOCKER_LOG\"\nif [ \"$1\" = info ]; then printf 'linux/amd64\\n'; else printf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'; fi\n").unwrap();
    fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
    let node = fake_bin.join("node");
    fs::write(
        &node,
        b"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CASTOR_FAKE_NODE_LOG\"\nexit 1\n",
    )
    .unwrap();
    fs::set_permissions(&node, fs::Permissions::from_mode(0o755)).unwrap();
    let output = Command::new(&exe)
        .args(["run", "--project"])
        .arg(&project)
        .arg("--task-spec")
        .arg(&spec)
        .args(["--model", "local-ollama", "--state-root"])
        .arg(root.path().join("state"))
        .env("HOME", root.path())
        .env("PATH", format!("{}:/usr/bin:/bin", fake_bin.display()))
        .env("CASTOR_FAKE_DOCKER_LOG", &docker_log)
        .env("CASTOR_FAKE_NODE_LOG", &node_log)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let envelope: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(envelope["launcher_status"], "PREFLIGHT_FAILED");
    assert_eq!(envelope["model_calls"], 0);
    assert_eq!(envelope["cleanup_status"], "CLEAN");
    assert!(
        !node_log.exists(),
        "model metadata was contacted before release preflight"
    );
    let calls = fs::read_to_string(&docker_log).unwrap_or_default();
    assert!(
        !calls.contains("build"),
        "controller built from source: {calls}"
    );
    let developer_override = Command::new(&exe)
        .args(["run", "--project"])
        .arg(&project)
        .arg("--task-spec")
        .arg(&spec)
        .args(["--model", "local-ollama", "--state-root"])
        .arg(root.path().join("override-state"))
        .env("HOME", root.path())
        .env("PATH", format!("{}:/usr/bin:/bin", fake_bin.display()))
        .env("CASTOR_DEVELOPER_SOURCE_CHECKOUT", "1")
        .env("CASTOR_FAKE_DOCKER_LOG", &docker_log)
        .env("CASTOR_FAKE_NODE_LOG", &node_log)
        .output()
        .unwrap();
    assert_eq!(developer_override.status.code(), Some(2));
    let override_envelope: Value = serde_json::from_slice(&developer_override.stdout).unwrap();
    assert_eq!(override_envelope["launcher_status"], "PREFLIGHT_FAILED");
    assert_eq!(override_envelope["model_calls"], 0);
    assert!(!node_log.exists(), "developer flag reached model script");
    assert!(!fs::read_to_string(&docker_log)
        .unwrap_or_default()
        .contains("build"));
}

#[test]
fn staged_script_rehashes_opened_bytes_and_survives_source_replacement() {
    let (root, exe) = fixture();
    let installed = InstalledRelease::load_at(&exe).unwrap();
    let destination = root.path().join("private-model-pin.mjs");
    installed
        .stage_script(HostScript::ModelPin, &destination)
        .unwrap();
    assert_eq!(fs::read(&destination).unwrap(), b"model pin");
    fs::write(
        root.path().join("libexec/castor/model_pin.mjs"),
        b"untrusted",
    )
    .unwrap();
    assert_eq!(fs::read(&destination).unwrap(), b"model pin");
    assert!(installed
        .stage_script(HostScript::ModelPin, &root.path().join("other.mjs"))
        .is_err());
}

#[test]
fn installed_pack_binds_versioned_carrier_instead_of_legacy_tag() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;
    let (root, exe) = fixture();
    fs::copy(env!("CARGO_BIN_EXE_castor"), root.path().join("bin/castor")).unwrap();
    fs::set_permissions(
        root.path().join("bin/castor"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let manifest_path = release_file(root.path());
    let mut manifest: Value = serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    manifest["host"]["castor_sha256"] =
        json!(digest(&fs::read(root.path().join("bin/castor")).unwrap()));
    fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let project = root.path().join("project");
    fs::create_dir(&project).unwrap();
    fs::write(project.join("hello.txt"), b"hello\n").unwrap();
    for args in [
        vec!["init", "-q"],
        vec!["add", "hello.txt"],
        vec![
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "init",
        ],
    ] {
        assert!(Command::new("git")
            .args(args)
            .current_dir(&project)
            .status()
            .unwrap()
            .success());
    }
    let spec = root.path().join("spec.json");
    fs::write(&spec, br#"{"schema_version":1,"task_prompt":"Fix hello","verification_command":["cat","hello.txt"]}"#).unwrap();
    let fake_bin = root.path().join("fake-bin");
    fs::create_dir(&fake_bin).unwrap();
    let docker = fake_bin.join("docker");
    let docker_log = root.path().join("docker.log");
    fs::write(
        &docker,
        br#"#!/bin/sh
printf '%s\n' "$*" >> "$CASTOR_FAKE_DOCKER_LOG"
if [ "$1" = info ]; then printf 'linux/amd64\n'; exit 0; fi
case "$5" in
  *controller@*) id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;;
  *carrier@*) id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb;;
  *verifier@*) id=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc;;
  *one-shot-*) id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb;;
  *) exit 1;;
esac
if [ "$4" = '{{json .}}' ]; then
  printf '{"Id":"sha256:%s","RepoDigests":["%s"],"Os":"linux","Architecture":"amd64"}\n' "$id" "$5"
else
  printf 'sha256:%s\n' "$id"
fi
"#,
    )
    .unwrap();
    let pins = &manifest["images"]["linux/amd64"];
    let receipt = json!({
        "release_version":"0.1.0", "source_revision":"c".repeat(40), "engine_arch":"amd64",
        "controller_ref":pins["controller"]["reference"], "carrier_ref":pins["carrier"]["reference"],
        "verifier_ref":pins["verifier"]["reference"], "controller_id":format!("sha256:{}", "a".repeat(64)),
        "carrier_id":format!("sha256:{}", "b".repeat(64)), "verifier_id":format!("sha256:{}", "c".repeat(64)),
        "carrier_tag":pins["carrier_tag"]
    });
    let runtime_dir = root.path().join(".castor/runtime");
    fs::create_dir_all(&runtime_dir).unwrap();
    fs::write(
        runtime_dir.join("0.1.0-linux-amd64.json"),
        serde_json::to_vec(&receipt).unwrap(),
    )
    .unwrap();
    fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
    let bundle = root.path().join("bundle");
    let output = Command::new(&exe)
        .args(["pack", "--project"])
        .arg(&project)
        .arg("--task-spec")
        .arg(&spec)
        .arg("--out")
        .arg(&bundle)
        .env("HOME", root.path())
        .env("PATH", format!("{}:/usr/bin:/bin", fake_bin.display()))
        .env("CASTOR_FAKE_DOCKER_LOG", &docker_log)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let packed: Value =
        serde_json::from_slice(&fs::read(bundle.join("manifest.json")).unwrap()).unwrap();
    assert_eq!(
        packed["carrier_base_image"],
        format!(
            "substratum/castor-pi-carrier:one-shot-0.1.0@sha256:{}",
            "b".repeat(64)
        )
    );
    let calls = fs::read_to_string(&docker_log).unwrap();
    assert!(calls.contains("one-shot-0.1.0"), "{calls}");
    assert!(!calls.contains("carrier:v1"), "{calls}");
    fs::write(&docker, b"#!/bin/sh\nif [ \"$1\" = info ]; then printf 'windows/amd64\\n'; else printf 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n'; fi\n").unwrap();
    let rejected = Command::new(&exe)
        .args(["pack", "--project"])
        .arg(&project)
        .arg("--task-spec")
        .arg(&spec)
        .arg("--out")
        .arg(root.path().join("windows-bundle"))
        .env("HOME", root.path())
        .env("PATH", format!("{}:/usr/bin:/bin", fake_bin.display()))
        .output()
        .unwrap();
    assert_eq!(rejected.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&rejected.stderr).contains("Linux Docker Engine"));
}

#[test]
fn installed_prepare_reports_missing_node_explicitly_before_docker() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;
    let (root, exe) = fixture();
    fs::copy(env!("CARGO_BIN_EXE_castor"), root.path().join("bin/castor")).unwrap();
    fs::set_permissions(
        root.path().join("bin/castor"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let manifest_path = release_file(root.path());
    let mut manifest: Value = serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    manifest["host"]["castor_sha256"] =
        json!(digest(&fs::read(root.path().join("bin/castor")).unwrap()));
    fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let empty_path = root.path().join("empty-path");
    fs::create_dir(&empty_path).unwrap();
    let output = Command::new(&exe)
        .args(["runtime", "prepare"])
        .env("HOME", root.path())
        .env("PATH", &empty_path)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("Node.js 22"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
