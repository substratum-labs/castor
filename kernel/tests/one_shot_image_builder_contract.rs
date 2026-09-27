use castor_kernel::one_shot::image::StagedSnapshot;
use castor_kernel::one_shot::manifest::TaskManifest;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::fs;
use std::process::Command;

#[test]
fn verified_archive_builds_digest_addressed_read_only_workspace() {
    let carrier = "substratum/castor-pi-carrier:v1";
    let inspect = Command::new("docker")
        .args(["image", "inspect", "--format", "{{.Id}}", carrier])
        .output()
        .expect("inspect locally built Pi carrier");
    assert!(inspect.status.success(), "build the Pi carrier first");
    let carrier_digest = String::from_utf8_lossy(&inspect.stdout).trim().to_owned();
    assert!(carrier_digest.starts_with("sha256:"));

    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    fs::create_dir(&source).unwrap();
    fs::write(source.join("defect.txt"), b"failing fixture\n").unwrap();
    let archive = root.path().join("snapshot.tar");
    let tar = Command::new("tar")
        .arg("-cf")
        .arg(&archive)
        .arg("-C")
        .arg(&source)
        .arg(".")
        .output()
        .unwrap();
    assert!(tar.status.success());
    let archive_sha256 = format!("{:x}", Sha256::digest(fs::read(&archive).unwrap()));
    let manifest_path = root.path().join("manifest.json");
    fs::write(
        &manifest_path,
        serde_json::to_vec(&json!({
            "task_id": "task-derived-image-contract",
            "idempotency_key": "derived-image-contract-1",
            "carrier_base_image": format!("{carrier}@{carrier_digest}"),
            "workspace_snapshot_path": "snapshot.tar",
            "workspace_snapshot_sha256": archive_sha256,
            "task_prompt": "Repair the failing fixture",
            "verification_command": ["sh", "-c", "test -f defect.txt"]
        }))
        .unwrap(),
    )
    .unwrap();

    let manifest = TaskManifest::read(&manifest_path).unwrap();
    let snapshot = manifest.validate_snapshot(&manifest_path).unwrap();
    let staged = StagedSnapshot::stage(snapshot, &manifest.workspace_snapshot_path).unwrap();
    let derived_digest = staged.build(&manifest.carrier_base_image).unwrap();
    assert!(derived_digest.starts_with("sha256:"));
    let inspect_derived = Command::new("docker")
        .args(["image", "inspect", "--format", "{{.Id}}", &derived_digest])
        .output()
        .unwrap();
    assert!(inspect_derived.status.success());
    assert_eq!(
        String::from_utf8_lossy(&inspect_derived.stdout).trim(),
        derived_digest
    );
    let read = Command::new("docker")
        .args([
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10001:10001",
            &derived_digest,
            "sh",
            "-c",
            "cat /workspace/defect.txt",
        ])
        .output()
        .unwrap();
    assert!(
        read.status.success(),
        "{}",
        String::from_utf8_lossy(&read.stderr)
    );
    assert_eq!(read.stdout, b"failing fixture\n");
}
