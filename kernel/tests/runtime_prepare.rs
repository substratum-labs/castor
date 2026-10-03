use castor_kernel::one_shot::install::InstalledRelease;
use castor_kernel::one_shot::runtime_prepare::{
    prepare, revalidate, EngineOps, EnginePlatform, ImageObservation,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::cell::RefCell;
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn pin(name: &str, hex: char) -> serde_json::Value {
    json!({"reference": format!("ghcr.io/substratum-labs/{name}@sha256:{}", hex.to_string().repeat(64))})
}
fn installed() -> (tempfile::TempDir, InstalledRelease) {
    let root = tempfile::tempdir().unwrap();
    for dir in ["bin", "libexec/castor", "share/castor"] {
        fs::create_dir_all(root.path().join(dir)).unwrap();
    }
    let exe = root.path().join("bin/castor");
    fs::write(&exe, b"binary").unwrap();
    fs::write(root.path().join("libexec/castor/model_pin.mjs"), b"pin").unwrap();
    fs::write(
        root.path().join("libexec/castor/ollama_model_adapter.mjs"),
        b"adapter",
    )
    .unwrap();
    let images = json!({
        "controller": pin("controller", 'a'), "carrier": pin("carrier", 'b'),
        "verifier": {"reference": format!("docker.io/library/python:3.12-slim@sha256:{}", "c".repeat(64))},
        "carrier_tag": "substratum/castor-pi-carrier:one-shot-0.1.0"
    });
    let body = json!({"schema_version":1,"release_version":"0.1.0","source_revision":"d".repeat(40),
        "host":{"os": if cfg!(target_os="macos") {"darwin"} else {"linux"},
            "arch": if cfg!(target_arch="aarch64") {"arm64"} else {"amd64"},
            "castor_sha256":sha(b"binary"),"model_pin_sha256":sha(b"pin"),"adapter_sha256":sha(b"adapter")},
        "images":{"linux/amd64":images,"linux/arm64":images}});
    fs::write(
        root.path().join("share/castor/release.json"),
        serde_json::to_vec(&body).unwrap(),
    )
    .unwrap();
    let release = InstalledRelease::load_at(&exe).unwrap();
    (root, release)
}

fn name_for_pin(reference: &str) -> &str {
    if reference.contains("controller") {
        "controller"
    } else if reference.contains("carrier") {
        "carrier"
    } else {
        "verifier"
    }
}

struct FakeEngine {
    platform: EnginePlatform,
    remote: HashMap<String, ImageObservation>,
    local: RefCell<HashMap<String, ImageObservation>>,
    tags: RefCell<HashMap<String, String>>,
    calls: RefCell<Vec<String>>,
    fail_pull_number: RefCell<Option<usize>>,
}
impl FakeEngine {
    fn new(release: &InstalledRelease, arch: &str) -> Self {
        let pins = release.pins(arch).unwrap();
        let mut remote = HashMap::new();
        for pin in [&pins.controller, &pins.carrier, &pins.verifier] {
            remote.insert(
                pin.reference.clone(),
                ImageObservation {
                    id: format!(
                        "sha256:{}",
                        match name_for_pin(&pin.reference) {
                            "controller" => "d",
                            "carrier" => "e",
                            _ => "f",
                        }
                        .repeat(64)
                    ),
                    repo_digests: vec![pin.reference.clone()],
                    os: "linux".into(),
                    arch: arch.into(),
                },
            );
        }
        Self {
            platform: EnginePlatform {
                os: "linux".into(),
                arch: arch.into(),
            },
            remote,
            local: RefCell::new(HashMap::new()),
            tags: RefCell::new(HashMap::new()),
            calls: RefCell::new(Vec::new()),
            fail_pull_number: RefCell::new(None),
        }
    }
    fn calls(&self) -> Vec<String> {
        self.calls.borrow().clone()
    }
    fn clear_calls(&self) {
        self.calls.borrow_mut().clear();
    }
}
impl EngineOps for FakeEngine {
    fn platform(&self) -> std::io::Result<EnginePlatform> {
        self.calls.borrow_mut().push("platform".into());
        Ok(self.platform.clone())
    }
    fn pull(&self, reference: &str) -> std::io::Result<()> {
        self.calls.borrow_mut().push(format!("pull {reference}"));
        let n = self
            .calls
            .borrow()
            .iter()
            .filter(|call| call.starts_with("pull "))
            .count();
        if *self.fail_pull_number.borrow() == Some(n) {
            return Err(std::io::Error::other("pull interrupted"));
        }
        let image = self.remote.get(reference).unwrap().clone();
        self.local.borrow_mut().insert(reference.into(), image);
        Ok(())
    }
    fn inspect(&self, reference: &str) -> std::io::Result<Option<ImageObservation>> {
        self.calls.borrow_mut().push(format!("inspect {reference}"));
        if let Some(id) = self.tags.borrow().get(reference) {
            return Ok(Some(ImageObservation {
                id: id.clone(),
                repo_digests: vec![],
                os: "linux".into(),
                arch: self.platform.arch.clone(),
            }));
        }
        Ok(self.local.borrow().get(reference).cloned())
    }
    fn tag(&self, id: &str, tag: &str) -> std::io::Result<()> {
        self.calls.borrow_mut().push(format!("tag {id} {tag}"));
        self.tags.borrow_mut().insert(tag.into(), id.into());
        Ok(())
    }
}

fn marker(root: &std::path::Path) -> PathBuf {
    root.join("runtime/0.1.0-linux-amd64.json")
}

#[test]
fn prepare_is_idempotent_and_revalidate_is_read_only() {
    let (_fixture, release) = installed();
    let state = tempfile::tempdir().unwrap();
    let engine = FakeEngine::new(&release, "amd64");
    let first = prepare(&release, &engine, state.path()).unwrap();
    assert_eq!(first.carrier_id, format!("sha256:{}", "e".repeat(64)));
    assert!(marker(state.path()).exists());
    assert_eq!(
        engine
            .calls()
            .iter()
            .filter(|call| call.starts_with("pull "))
            .count(),
        3
    );
    engine.clear_calls();
    let second = prepare(&release, &engine, state.path()).unwrap();
    assert_eq!(second.carrier_id, first.carrier_id);
    assert!(!engine
        .calls()
        .iter()
        .any(|call| call.starts_with("pull ") || call.starts_with("tag ")));
    engine.clear_calls();
    revalidate(&release, &engine, state.path()).unwrap();
    assert!(!engine
        .calls()
        .iter()
        .any(|call| call.starts_with("pull ") || call.starts_with("tag ")));
    engine.tags.borrow_mut().insert(
        first.carrier_tag.clone(),
        format!("sha256:{}", "9".repeat(64)),
    );
    assert!(revalidate(&release, &engine, state.path()).is_err());
}

#[test]
fn interrupted_prepare_never_writes_ready_marker() {
    let (_fixture, release) = installed();
    let state = tempfile::tempdir().unwrap();
    let engine = FakeEngine::new(&release, "amd64");
    *engine.fail_pull_number.borrow_mut() = Some(2);
    assert!(prepare(&release, &engine, state.path()).is_err());
    assert!(!marker(state.path()).exists());
    *engine.fail_pull_number.borrow_mut() = None;
    engine.clear_calls();
    prepare(&release, &engine, state.path()).unwrap();
    assert!(marker(state.path()).exists());
}

#[test]
fn rejects_unsupported_platform_and_malformed_observed_id_before_ready() {
    let (_fixture, release) = installed();
    let state = tempfile::tempdir().unwrap();
    let mut engine = FakeEngine::new(&release, "amd64");
    engine.platform.arch = "s390x".into();
    assert!(prepare(&release, &engine, state.path()).is_err());
    assert!(!engine.calls().iter().any(|call| call.starts_with("pull ")));
    let mut engine = FakeEngine::new(&release, "amd64");
    let controller = &release.pins("amd64").unwrap().controller.reference;
    engine.remote.get_mut(controller).unwrap().id = "malformed-image-id".into();
    assert!(prepare(&release, &engine, state.path()).is_err());
    assert!(!marker(state.path()).exists());
}

#[test]
fn docker_hub_library_alias_preserves_verifier_digest_identity() {
    let (_fixture, release) = installed();
    let state = tempfile::tempdir().unwrap();
    let mut engine = FakeEngine::new(&release, "amd64");
    let verifier = &release.pins("amd64").unwrap().verifier.reference;
    engine.remote.get_mut(verifier).unwrap().repo_digests =
        vec![format!("python@sha256:{}", "c".repeat(64))];
    assert!(prepare(&release, &engine, state.path()).is_ok());
    engine.remote.get_mut(verifier).unwrap().repo_digests =
        vec![format!("python@sha256:{}", "d".repeat(64))];
    engine
        .local
        .borrow_mut()
        .get_mut(verifier)
        .unwrap()
        .repo_digests = vec![format!("python@sha256:{}", "d".repeat(64))];
    assert!(revalidate(&release, &engine, state.path()).is_err());
}

#[test]
fn explicit_prepare_refreshes_ids_after_engine_store_change() {
    let (_fixture, release) = installed();
    let state = tempfile::tempdir().unwrap();
    let engine = FakeEngine::new(&release, "amd64");
    let old = prepare(&release, &engine, state.path()).unwrap();
    let pins = release.pins("amd64").unwrap();
    for (pin, digit) in [
        (&pins.controller, "1"),
        (&pins.carrier, "2"),
        (&pins.verifier, "3"),
    ] {
        engine
            .local
            .borrow_mut()
            .get_mut(&pin.reference)
            .unwrap()
            .id = format!("sha256:{}", digit.repeat(64));
    }
    let new_carrier = format!("sha256:{}", "2".repeat(64));
    engine
        .tags
        .borrow_mut()
        .insert(pins.carrier_tag.clone(), new_carrier.clone());
    assert!(revalidate(&release, &engine, state.path()).is_err());
    engine.clear_calls();
    let refreshed = prepare(&release, &engine, state.path()).unwrap();
    assert_ne!(refreshed.controller_id, old.controller_id);
    assert_eq!(refreshed.carrier_id, new_carrier);
    assert!(!engine
        .calls()
        .iter()
        .any(|call| call.starts_with("pull ") || call.starts_with("tag ")));
    assert_eq!(
        revalidate(&release, &engine, state.path())
            .unwrap()
            .carrier_id,
        new_carrier
    );
}

#[test]
fn digest_mismatch_cannot_be_repaired_by_a_matching_local_id() {
    let (_fixture, release) = installed();
    let state = tempfile::tempdir().unwrap();
    let engine = FakeEngine::new(&release, "amd64");
    let controller = &release.pins("amd64").unwrap().controller.reference;
    engine.remote.get(controller).unwrap();
    engine.local.borrow_mut().insert(
        controller.clone(),
        ImageObservation {
            id: format!("sha256:{}", "d".repeat(64)),
            repo_digests: vec![format!(
                "ghcr.io/substratum-labs/controller@sha256:{}",
                "9".repeat(64)
            )],
            os: "linux".into(),
            arch: "amd64".into(),
        },
    );
    assert!(prepare(&release, &engine, state.path()).is_err());
    assert!(!marker(state.path()).exists());
}
