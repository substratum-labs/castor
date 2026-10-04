use castor_kernel::one_shot::image::StagedSnapshot;
use castor_kernel::one_shot::manifest::ValidatedSnapshot;
use castor_kernel::one_shot::oci_layout::{import_docker_archive, import_layout, verify_layout};
use castor_kernel::one_shot::runtime_prepare::{revalidate_carrier_layout, PreparedRuntime};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;
use tar::{Builder, EntryType, Header};

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn append(builder: &mut Builder<Vec<u8>>, name: &str, bytes: &[u8]) {
    let mut header = Header::new_gnu();
    header.set_path(name).unwrap();
    header.set_size(bytes.len() as u64);
    header.set_mode(0o644);
    header.set_cksum();
    builder.append(&header, bytes).unwrap();
}

fn image_tar(arch: &str, corrupt_config: bool, symlink: bool) -> (Vec<u8>, String, String) {
    let config = serde_json::to_vec(&json!({
        "architecture":arch,"os":"linux",
        "config":{"Labels":{"org.opencontainers.image.revision":"a".repeat(40)}}
    }))
    .unwrap();
    let config_digest = digest(&config);
    let manifest = serde_json::to_vec(&json!({
        "schemaVersion":2,
        "mediaType":"application/vnd.oci.image.manifest.v1+json",
        "config":{"mediaType":"application/vnd.oci.image.config.v1+json",
                  "digest":config_digest,"size":config.len()},
        "layers":[]
    }))
    .unwrap();
    let manifest_digest = digest(&manifest);
    let index = serde_json::to_vec(&json!({
        "schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json",
        "manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json",
                      "digest":manifest_digest,"size":manifest.len()}]
    }))
    .unwrap();
    let mut builder = Builder::new(Vec::new());
    append(
        &mut builder,
        "oci-layout",
        br#"{"imageLayoutVersion":"1.0.0"}"#,
    );
    append(&mut builder, "index.json", &index);
    append(
        &mut builder,
        &format!("blobs/sha256/{}", &manifest_digest[7..]),
        &manifest,
    );
    if symlink {
        let mut header = Header::new_gnu();
        header
            .set_path(format!("blobs/sha256/{}", &config_digest[7..]))
            .unwrap();
        header.set_entry_type(EntryType::Symlink);
        header.set_link_name("/etc/passwd").unwrap();
        header.set_size(0);
        header.set_cksum();
        builder.append(&header, io::empty()).unwrap();
    } else {
        let contents: &[u8] = if corrupt_config { b"corrupt" } else { &config };
        append(
            &mut builder,
            &format!("blobs/sha256/{}", &config_digest[7..]),
            contents,
        );
    }
    (
        builder.into_inner().unwrap(),
        manifest_digest,
        config_digest,
    )
}

#[test]
fn imports_a_content_checked_layout_and_accepts_both_engine_id_forms() {
    let root = tempfile::tempdir().unwrap();
    let archive = root.path().join("image.tar");
    let layout = root.path().join("layout");
    let (tar, manifest_id, config_id) = image_tar("arm64", false, false);
    fs::write(&archive, tar).unwrap();
    let actual = import_layout(
        &archive,
        &layout,
        &manifest_id,
        "arm64",
        Some(&"a".repeat(40)),
    )
    .unwrap();
    assert_eq!(actual, manifest_id);
    assert_eq!(
        verify_layout(&layout, &config_id, "arm64", Some(&"a".repeat(40))).unwrap(),
        manifest_id
    );
}

#[test]
fn refuses_corrupt_or_wrong_image_before_publishing_it() {
    let root = tempfile::tempdir().unwrap();
    for (name, arch, corrupt, symlink) in [
        ("corrupt", "arm64", true, false),
        ("symlink", "arm64", false, true),
        ("wrong-arch", "amd64", false, false),
    ] {
        let archive = root.path().join(format!("{name}.tar"));
        let layout = root.path().join(name);
        let (tar, manifest_id, _) = image_tar(arch, corrupt, symlink);
        fs::write(&archive, tar).unwrap();
        assert!(import_layout(
            &archive,
            &layout,
            &manifest_id,
            "arm64",
            Some(&"a".repeat(40))
        )
        .is_err());
        assert!(!layout.exists(), "invalid layout was published: {name}");
    }
}

#[test]
fn prepared_layout_is_bound_to_the_receipt_and_tampering_fails_closed() {
    let root = tempfile::tempdir().unwrap();
    let (tar, manifest_id, _) = image_tar("arm64", false, false);
    let archive = root.path().join("carrier.tar");
    fs::write(&archive, tar).unwrap();
    let receipt = PreparedRuntime {
        release_version: "0.1.0".into(),
        source_revision: "a".repeat(40),
        engine_arch: "arm64".into(),
        controller_ref: "unused".into(),
        carrier_ref: "unused".into(),
        verifier_ref: "unused".into(),
        controller_id: "unused".into(),
        carrier_id: manifest_id.clone(),
        verifier_id: "unused".into(),
        carrier_tag: "unused".into(),
    };
    let layout = root.path().join("runtime").join(format!(
        "carrier-oci-0.1.0-linux-arm64-{}",
        &manifest_id[7..]
    ));
    import_layout(
        &archive,
        &layout,
        &manifest_id,
        "arm64",
        Some(&"a".repeat(40)),
    )
    .unwrap();
    assert_eq!(
        revalidate_carrier_layout(&receipt, root.path())
            .unwrap()
            .manifest_digest,
        manifest_id
    );
    let index = layout.join("index.json");
    fs::set_permissions(&index, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(index, b"changed").unwrap();
    assert!(revalidate_carrier_layout(&receipt, root.path()).is_err());
}

#[test]
fn classic_docker_archive_converts_to_verified_oci_without_a_registry_base() {
    let root = tempfile::tempdir().unwrap();
    let layer = b"a deliberately small layer tar fixture";
    let layer_digest = digest(layer);
    let config = serde_json::to_vec(&json!({
        "architecture":"amd64","os":"linux",
        "rootfs":{"type":"layers","diff_ids":[layer_digest]},
        "config":{"Labels":{"org.opencontainers.image.revision":"b".repeat(40)}}
    }))
    .unwrap();
    let config_digest = digest(&config);
    let config_name = format!("{}.json", &config_digest[7..]);
    let saved_manifest = serde_json::to_vec(&json!([{
        "Config":config_name,"RepoTags":["local/carrier:v1"],
        "Layers":["layer-1/layer.tar"]
    }]))
    .unwrap();
    let mut builder = Builder::new(Vec::new());
    append(&mut builder, "manifest.json", &saved_manifest);
    append(&mut builder, &config_name, &config);
    append(&mut builder, "layer-1/layer.tar", layer);
    let archive = root.path().join("classic.tar");
    fs::write(&archive, builder.into_inner().unwrap()).unwrap();
    let layout = root.path().join("layout");
    let manifest_digest = import_docker_archive(
        &archive,
        &layout,
        &config_digest,
        "amd64",
        Some(&"b".repeat(40)),
    )
    .unwrap();
    assert_eq!(
        verify_layout(&layout, &config_digest, "amd64", Some(&"b".repeat(40))).unwrap(),
        manifest_digest
    );
    let wrong = root.path().join("wrong");
    assert!(import_docker_archive(&archive, &wrong, &digest(b"wrong"), "amd64", None).is_err());
    assert!(!wrong.exists());
}

#[test]
#[ignore = "requires a local Docker Engine with the T-389 candidate carrier loaded"]
fn buildx_consumes_carrier_as_local_oci_and_loads_a_derived_image() {
    let tag = "substratum/castor-pi-carrier:one-shot-0.1.0-rc1";
    let inspect = Command::new("docker")
        .args(["image", "inspect", "--format", "{{.Id}}", tag])
        .output()
        .unwrap();
    assert!(inspect.status.success());
    let id = String::from_utf8(inspect.stdout).unwrap().trim().to_owned();
    let root = tempfile::tempdir().unwrap();
    let mut builder = Builder::new(Vec::new());
    append(
        &mut builder,
        "t389-unique.txt",
        b"offline BuildKit fixture\n",
    );
    let path = root.path().join("project.tar");
    fs::write(&path, builder.into_inner().unwrap()).unwrap();
    let snapshot = ValidatedSnapshot {
        file: fs::File::open(&path).unwrap(),
        sha256: digest(&fs::read(&path).unwrap()),
    };
    let staged = StagedSnapshot::stage(snapshot, &path).unwrap();
    let derived = staged.build(&format!("{tag}@{id}")).unwrap();
    let inspect = Command::new("docker")
        .args(["image", "inspect", "--format", "{{.Id}}", &derived])
        .output()
        .unwrap();
    assert!(inspect.status.success());
    assert_eq!(String::from_utf8(inspect.stdout).unwrap().trim(), derived);
    assert!(Command::new("docker")
        .args(["image", "rm", &derived])
        .status()
        .unwrap()
        .success());
}
