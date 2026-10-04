//! Opt-in Engine test: CASTOR_TEST_CARRIER_TAG must name a disposable local Pi tag.
use castor_kernel::one_shot::image::StagedSnapshot;
use castor_kernel::one_shot::manifest::ValidatedSnapshot;
use std::fs;
use std::io::{self, Read};
use std::process::Command;

fn docker(args: &[&str]) -> String {
    let output = Command::new("docker").args(args).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

#[test]
#[ignore = "requires CASTOR_TEST_CARRIER_TAG and a local Docker Engine"]
fn snapshot_image_preserves_carrier_and_readonly_agent_file() {
    let tag = std::env::var("CASTOR_TEST_CARRIER_TAG").unwrap();
    let base_id = docker(&["image", "inspect", "--format", "{{.Id}}", &tag]);
    let root = tempfile::tempdir().unwrap();
    let archive = root.path().join("snapshot.tar");
    let mut tar = tar::Builder::new(fs::File::create(&archive).unwrap());
    let mut header = tar::Header::new_gnu();
    header.set_path("t389-probe.txt").unwrap();
    header.set_size(9);
    header.set_mode(0o644);
    header.set_cksum();
    tar.append(&header, &b"copy-only"[..]).unwrap();
    tar.finish().unwrap();
    drop(tar);
    let staged = StagedSnapshot::stage(
        ValidatedSnapshot {
            file: fs::File::open(&archive).unwrap(),
            sha256: String::new(),
        },
        &archive,
    )
    .unwrap();
    let derived = staged.build(&format!("{tag}@{base_id}")).unwrap();
    let cid = docker(&["create", "--read-only", "--network", "none", &derived]);
    let output = Command::new("docker")
        .args(["cp", &format!("{cid}:/workspace/t389-probe.txt"), "-"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let mut reader = tar::Archive::new(io::Cursor::new(output.stdout));
    let mut entry = reader.entries().unwrap().next().unwrap().unwrap();
    assert_eq!(
        (
            entry.header().uid().unwrap(),
            entry.header().gid().unwrap(),
            entry.header().mode().unwrap()
        ),
        (10001, 10001, 0o555)
    );
    let mut content = String::new();
    entry.read_to_string(&mut content).unwrap();
    assert_eq!(content, "copy-only");
    docker(&["rm", &cid]);
    docker(&["image", "rm", &derived]);
}
