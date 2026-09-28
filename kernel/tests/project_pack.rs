use serde_json::json;
use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::Path;
use std::process::Command;

fn pack(root: &Path, output: &Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_castor"))
        .args(["pack", "--project"])
        .arg(root.join("source"))
        .arg("--task-spec")
        .arg(root.join("spec.json"))
        .arg("--out")
        .arg(output)
        .output()
        .unwrap()
}

fn pack_with_fake_carrier(root: &Path, output: &Path) -> std::process::Output {
    let bin = root.join("bin");
    fs::create_dir_all(&bin).unwrap();
    let docker = bin.join("docker");
    fs::write(&docker, b"#!/bin/sh\nprintf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'\n").unwrap();
    fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
    Command::new(env!("CARGO_BIN_EXE_castor"))
        .args(["pack", "--project"])
        .arg(root.join("source"))
        .arg("--task-spec")
        .arg(root.join("spec.json"))
        .arg("--out")
        .arg(output)
        .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
        .output()
        .unwrap()
}

fn fixture(root: &Path) {
    let source = root.join("source");
    fs::create_dir(&source).unwrap();
    fs::write(source.join("hello.txt"), b"hello\n").unwrap();
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
            .current_dir(&source)
            .status()
            .unwrap()
            .success());
    }
    fs::write(
        root.join("spec.json"),
        serde_json::to_vec(&json!({
            "schema_version": 1,
            "task_prompt": "Fix hello",
            "verification_command": ["cat", "hello.txt"]
        }))
        .unwrap(),
    )
    .unwrap();
}

#[test]
fn pack_publishes_repeatable_bundle_without_changing_source() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    let spec = root.path().join("spec.json");
    let output = root.path().join("bundle");
    let before = fs::read(source.join("hello.txt")).unwrap();
    let invoke = || {
        Command::new(env!("CARGO_BIN_EXE_castor"))
            .args(["pack", "--project"])
            .arg(&source)
            .arg("--task-spec")
            .arg(&spec)
            .arg("--out")
            .arg(&output)
            .output()
            .unwrap()
    };
    let first = invoke();
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let manifest = fs::read(output.join("manifest.json")).unwrap();
    let archive = fs::read(output.join("workspace.tar")).unwrap();
    let second = invoke();
    assert!(
        second.status.success(),
        "{}",
        String::from_utf8_lossy(&second.stderr)
    );
    assert_eq!(fs::read(output.join("manifest.json")).unwrap(), manifest);
    assert_eq!(fs::read(output.join("workspace.tar")).unwrap(), archive);
    assert_eq!(fs::read(source.join("hello.txt")).unwrap(), before);
}

#[test]
fn pack_rejects_dirty_and_untracked_input_even_when_excluded() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    fs::write(root.path().join("spec.json"), serde_json::to_vec(&json!({
        "schema_version": 1, "task_prompt": "Fix hello", "verification_command": ["cat", "hello.txt"],
        "exclude_paths": ["hello.txt"]
    })).unwrap()).unwrap();
    fs::write(source.join("hello.txt"), b"changed\n").unwrap();
    let out = root.path().join("bundle");
    let dirty = pack(root.path(), &out);
    assert_eq!(dirty.status.code(), Some(2));
    assert!(!out.exists());
    fs::write(source.join("hello.txt"), b"hello\n").unwrap();
    fs::write(source.join("extra.txt"), b"untracked\n").unwrap();
    let untracked = pack(root.path(), &out);
    assert_eq!(untracked.status.code(), Some(2));
    assert!(!out.exists());
}

#[test]
fn pack_rejects_symlink_and_special_file_input() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    let file = source.join("hello.txt");
    fs::remove_file(&file).unwrap();
    symlink("/etc/passwd", &file).unwrap();
    let out = root.path().join("bundle");
    assert_eq!(pack(root.path(), &out).status.code(), Some(2));
    fs::remove_file(&file).unwrap();
    let fifo = std::ffi::CString::new(file.to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
    assert_eq!(pack(root.path(), &out).status.code(), Some(2));
}

#[test]
fn pack_rejects_unsafe_output_and_only_reuses_exact_bundle() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let out = root.path().join("bundle");
    symlink(root.path().join("source"), &out).unwrap();
    assert_eq!(pack(root.path(), &out).status.code(), Some(2));
    fs::remove_file(&out).unwrap();
    fs::create_dir(&out).unwrap();
    assert_eq!(pack(root.path(), &out).status.code(), Some(2));
    assert_eq!(fs::read_dir(&out).unwrap().count(), 0);
    fs::remove_dir(&out).unwrap();
    fs::create_dir(root.path().join("safe-parent")).unwrap();
    symlink(
        root.path().join("safe-parent"),
        root.path().join("linked-parent"),
    )
    .unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("linked-parent/bundle"))
            .status
            .code(),
        Some(2)
    );
    assert_eq!(
        fs::read_dir(root.path().join("safe-parent"))
            .unwrap()
            .count(),
        0
    );
}

#[test]
fn project_frontend_rejects_test_opcodes_and_nonnull_limits() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let output = Command::new(env!("CARGO_BIN_EXE_castor"))
        .args(["pack", "--allow-test-opcodes", "--project"])
        .arg(root.path().join("source"))
        .arg("--task-spec")
        .arg(root.path().join("spec.json"))
        .arg("--out")
        .arg(root.path().join("bundle"))
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    fs::write(root.path().join("spec.json"), serde_json::to_vec(&json!({
        "schema_version": 1, "task_prompt": "Fix hello", "verification_command": ["cat", "hello.txt"],
        "limits": {"calls": 4}
    })).unwrap()).unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("bundle")).status.code(),
        Some(2)
    );
}

#[test]
fn pack_identity_and_archive_change_with_asset_bytes() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    fs::create_dir(root.path().join("assets")).unwrap();
    fs::write(root.path().join("assets/check.txt"), b"one\n").unwrap();
    fs::write(root.path().join("assets/.env"), b"SECRET=value\n").unwrap();
    fs::write(root.path().join("spec.json"), serde_json::to_vec(&json!({
        "schema_version": 1, "task_prompt": "Fix hello", "verification_command": ["cat", "hello.txt"],
        "verification_assets": "assets"
    })).unwrap()).unwrap();
    let first = root.path().join("one");
    let second = root.path().join("two");
    assert!(pack(root.path(), &first).status.success());
    let first_manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(first.join("manifest.json")).unwrap()).unwrap();
    assert!(first_manifest.get("limits").is_none());
    assert_eq!(first_manifest["verification_timeout_seconds"], 300);
    let first_id = first_manifest["task_id"].as_str().unwrap().to_owned();
    let archive = fs::read(first.join("workspace.tar")).unwrap();
    let mut paths = Vec::new();
    for entry in tar::Archive::new(archive.as_slice()).entries().unwrap() {
        let entry = entry.unwrap();
        let header = entry.header();
        assert_eq!(header.mtime().unwrap(), 0);
        assert_eq!(header.uid().unwrap(), 0);
        assert_eq!(header.gid().unwrap(), 0);
        assert!(header.entry_type().is_file());
        assert_eq!(header.mode().unwrap(), 0o644);
        paths.push(entry.path().unwrap().to_string_lossy().to_string());
    }
    assert_eq!(
        paths,
        [".castor_verification_assets/check.txt", "hello.txt"]
    );
    let receipt: serde_json::Value =
        serde_json::from_slice(&fs::read(first.join("pack-receipt.json")).unwrap()).unwrap();
    assert!(receipt["excluded_entries"]
        .as_array()
        .unwrap()
        .iter()
        .any(|e| e["path"] == ".castor_verification_assets/.env"
            && e["reason"] == "default-exclusion"));
    fs::write(root.path().join("assets/check.txt"), b"two\n").unwrap();
    assert!(pack(root.path(), &second).status.success());
    let second_manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(second.join("manifest.json")).unwrap()).unwrap();
    assert_ne!(second_manifest["task_id"], first_id);
    assert_ne!(
        fs::read(first.join("workspace.tar")).unwrap(),
        fs::read(second.join("workspace.tar")).unwrap()
    );
}

#[test]
fn asset_executable_and_traversal_fail_closed() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    fs::create_dir(root.path().join("assets")).unwrap();
    let check = root.path().join("assets/check.sh");
    fs::write(&check, b"exit 0\n").unwrap();
    fs::set_permissions(&check, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(root.path().join("spec.json"), serde_json::to_vec(&json!({
        "schema_version": 1, "task_prompt": "Fix hello", "verification_command": ["cat", "hello.txt"],
        "verification_assets": "assets"
    })).unwrap()).unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("out")).status.code(),
        Some(2)
    );
    fs::set_permissions(&check, fs::Permissions::from_mode(0o644)).unwrap();
    fs::remove_file(&check).unwrap();
    symlink("/etc/passwd", &check).unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("out")).status.code(),
        Some(2)
    );
    fs::write(root.path().join("spec.json"), serde_json::to_vec(&json!({
        "schema_version": 1, "task_prompt": "Fix hello", "verification_command": ["cat", "hello.txt"],
        "verification_assets": "../outside"
    })).unwrap()).unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("out")).status.code(),
        Some(2)
    );
}

#[test]
fn git_hooks_filters_and_remote_helpers_are_never_run() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    let docker = root.path().join("docker");
    fs::write(&docker, b"#!/bin/sh\nprintf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'\n").unwrap();
    fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
    let marker = root.path().join("canary-ran");
    let script = root.path().join("canary.sh");
    fs::write(
        &script,
        format!("#!/bin/sh\ntouch '{}'\nexit 17\n", marker.display()),
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(
        source.join(".git/hooks/fsmonitor-watchman"),
        fs::read(&script).unwrap(),
    )
    .unwrap();
    fs::set_permissions(
        source.join(".git/hooks/fsmonitor-watchman"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    fs::write(
        root.path().join("git-remote-canary"),
        fs::read(&script).unwrap(),
    )
    .unwrap();
    fs::set_permissions(
        root.path().join("git-remote-canary"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    for args in [
        vec!["config", "core.fsmonitor", script.to_str().unwrap()],
        vec!["config", "filter.canary.clean", script.to_str().unwrap()],
        vec!["config", "filter.canary.smudge", script.to_str().unwrap()],
        vec!["config", "diff.canary.textconv", script.to_str().unwrap()],
    ] {
        assert!(Command::new("git")
            .args(args)
            .current_dir(&source)
            .status()
            .unwrap()
            .success());
    }
    let output = Command::new(env!("CARGO_BIN_EXE_castor"))
        .args(["pack", "--project"])
        .arg(&source)
        .arg("--task-spec")
        .arg(root.path().join("spec.json"))
        .arg("--out")
        .arg(root.path().join("out"))
        .env("GIT_EXEC_PATH", root.path())
        .env(
            "PATH",
            format!("{}:/usr/local/bin:/usr/bin:/bin", root.path().display()),
        )
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!marker.exists());
}

#[test]
fn exclusions_have_stable_reasons_and_do_not_skip_dirty_checks() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    fs::create_dir_all(source.join("pkg/node_modules")).unwrap();
    fs::create_dir_all(source.join("build_cache")).unwrap();
    fs::write(source.join("pkg/token.key"), b"secret\n").unwrap();
    fs::write(source.join("pkg/node_modules/x"), b"module\n").unwrap();
    fs::write(source.join("build_cache/x"), b"cache\n").unwrap();
    fs::write(source.join(".env"), b"env\n").unwrap();
    assert!(Command::new("git")
        .args(["add", "."])
        .current_dir(&source)
        .status()
        .unwrap()
        .success());
    assert!(Command::new("git")
        .args([
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "add files"
        ])
        .current_dir(&source)
        .status()
        .unwrap()
        .success());
    fs::write(root.path().join("spec.json"), serde_json::to_vec(&json!({
        "schema_version": 1, "task_prompt": "Fix hello", "verification_command": ["cat", "hello.txt"],
        "exclude_paths": ["build_cache/"]
    })).unwrap()).unwrap();
    let out = root.path().join("out");
    let result = pack(root.path(), &out);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let receipt: serde_json::Value =
        serde_json::from_slice(&fs::read(out.join("pack-receipt.json")).unwrap()).unwrap();
    let excluded = receipt["excluded_entries"].as_array().unwrap();
    assert!(excluded
        .iter()
        .any(|e| e["path"] == "build_cache/x" && e["reason"] == "spec-exclusion"));
    assert!(excluded
        .iter()
        .any(|e| e["path"] == "pkg/token.key" && e["reason"] == "default-exclusion"));
    assert!(excluded
        .iter()
        .any(|e| e["path"] == "pkg/node_modules/x" && e["reason"] == "default-exclusion"));
    assert!(excluded
        .iter()
        .any(|e| e["path"] == ".env" && e["reason"] == "default-exclusion"));
    fs::write(source.join("build_cache/x"), b"dirty\n").unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("second")).status.code(),
        Some(2)
    );
}

#[test]
fn output_inside_project_and_hardlinked_source_fail_closed() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    assert_eq!(
        pack(root.path(), &source.join("nested/out")).status.code(),
        Some(2)
    );
    fs::hard_link(source.join("hello.txt"), root.path().join("linked")).unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("out")).status.code(),
        Some(2)
    );
}

#[test]
fn ignored_asset_prefix_collision_is_rejected() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    fs::write(
        source.join(".git/info/exclude"),
        b".castor_verification_assets/\n",
    )
    .unwrap();
    fs::create_dir(source.join(".castor_verification_assets")).unwrap();
    fs::write(
        source.join(".castor_verification_assets/ignored"),
        b"hidden",
    )
    .unwrap();
    let result = pack(root.path(), &root.path().join("out"));
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&result.stderr).contains("collides"));
}

#[test]
fn output_parent_navigation_to_sibling_is_confined() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let out = root.path().join("source/../sibling");
    let result = pack(root.path(), &out);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(root.path().join("sibling/manifest.json").is_file());
}

#[test]
fn oversized_git_blob_fails_with_bounded_input_read() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    fs::write(source.join("large.bin"), vec![0x5a; 32 * 1024 * 1024 + 1]).unwrap();
    assert!(Command::new("git")
        .args(["add", "large.bin"])
        .current_dir(&source)
        .status()
        .unwrap()
        .success());
    assert!(Command::new("git")
        .args([
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "large blob"
        ])
        .current_dir(&source)
        .status()
        .unwrap()
        .success());
    let out = root.path().join("out");
    let result = pack(root.path(), &out);
    assert_eq!(result.status.code(), Some(2));
    assert!(!out.exists());
}

#[test]
fn unsupported_git_configuration_fails_before_object_lookup() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    let config = source.join(".git/config");
    let original = fs::read(&config).unwrap();
    assert!(
        pack_with_fake_carrier(root.path(), &root.path().join("baseline"))
            .status
            .success()
    );
    for hostile in [
        "\n[extensions]\npartialclone = origin\n",
        "\n[remote \"origin\"]\npromisor = true\n",
        "\n[core]\nsparseCheckout = true\n",
        "\n[index]\nsparse = true\n",
        "\n[remote \"origin\"]\npromisor = \"true\"\n",
        "\n[remote.origin]\npromisor = true\n",
        "\n[remote\t\"origin\"]\npromisor = true\n",
        "\n[remote \"origin\"]\npromisor = 2\n",
        "\n[index]\nsparse = 2\n",
        "\n[remote \"origin\"]\npromisor = tr\\\nue\n",
        "\n[core]\nsparseCheckout = \"yes\"\n",
        "\n[includeIf \"gitdir:./\"]\npath = /dev/null\n",
    ] {
        fs::write(&config, [original.as_slice(), hostile.as_bytes()].concat()).unwrap();
        assert_eq!(
            pack_with_fake_carrier(root.path(), &root.path().join("out"))
                .status
                .code(),
            Some(2)
        );
    }
    fs::write(&config, &original).unwrap();
    fs::create_dir_all(source.join(".git/objects/info")).unwrap();
    fs::write(
        source.join(".git/objects/info/alternates"),
        b"/private/tmp/objects\n",
    )
    .unwrap();
    assert_eq!(
        pack(root.path(), &root.path().join("out")).status.code(),
        Some(2)
    );
}

#[test]
fn group_or_other_execute_bits_do_not_make_nonexecutable_source_dirty() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    for mode in [0o654, 0o645] {
        fs::set_permissions(source.join("hello.txt"), fs::Permissions::from_mode(mode)).unwrap();
        let out = root.path().join(format!("out-{mode:o}"));
        let result = pack_with_fake_carrier(root.path(), &out);
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let tar_file = fs::File::open(out.join("workspace.tar")).unwrap();
        let mut tar = tar::Archive::new(tar_file);
        let header = tar.entries().unwrap().next().unwrap().unwrap();
        assert_eq!(header.header().mode().unwrap(), 0o644);
    }
}

#[test]
fn primary_worktree_config_and_gitdir_parent_symlink_fail_preflight() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    let out = root.path().join("out");
    assert!(
        pack_with_fake_carrier(root.path(), &root.path().join("baseline"))
            .status
            .success()
    );
    let config = source.join(".git/config");
    let original = fs::read(&config).unwrap();
    fs::write(
        &config,
        [
            original.as_slice(),
            b"\n[extensions]\nworktreeConfig = true\n",
        ]
        .concat(),
    )
    .unwrap();
    fs::write(
        source.join(".git/config.worktree"),
        b"[core]\nsparseCheckout = true\n",
    )
    .unwrap();
    assert_eq!(
        pack_with_fake_carrier(root.path(), &out).status.code(),
        Some(2)
    );
    fs::remove_file(source.join(".git/config.worktree")).unwrap();
    fs::write(&config, &original).unwrap();
    fs::rename(source.join(".git"), source.join("real-git")).unwrap();
    symlink("real-git", source.join("git-link")).unwrap();
    fs::write(source.join(".git"), b"gitdir: git-link\n").unwrap();
    let result = pack_with_fake_carrier(root.path(), &out);
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&result.stderr).contains("Not a directory"));
}

#[test]
fn dangling_common_directory_symlink_is_not_treated_as_absent() {
    let root = tempfile::tempdir().unwrap();
    fixture(root.path());
    let source = root.path().join("source");
    assert!(
        pack_with_fake_carrier(root.path(), &root.path().join("baseline"))
            .status
            .success()
    );
    symlink("missing-common", source.join(".git/commondir")).unwrap();
    let result = pack_with_fake_carrier(root.path(), &root.path().join("out"));
    assert_eq!(result.status.code(), Some(2));
    assert!(!String::from_utf8_lossy(&result.stderr).contains("Git plumbing failed"));
}

#[test]
fn canonical_identity_includes_effective_timeout_and_full_image_pin() {
    use castor_kernel::one_shot::project::spec::{
        canonical_spec_bytes, derive_task_identity, parse_and_validate_spec,
    };
    let root = tempfile::tempdir().unwrap();
    fs::write(root.path().join("spec.json"), serde_json::to_vec(&json!({
        "schema_version": 1, "task_prompt": "Fix hello", "verification_command": ["cat", "hello.txt"]
    })).unwrap()).unwrap();
    let spec = parse_and_validate_spec(&root.path().join("spec.json")).unwrap();
    assert_eq!(String::from_utf8(canonical_spec_bytes(&spec)).unwrap(),
        "{\"schema_version\":1,\"task_prompt\":\"Fix hello\",\"verification_command\":[\"cat\",\"hello.txt\"],\"verification_timeout_seconds\":300}");
    let a = derive_task_identity(
        &spec,
        "a",
        "b",
        "substratum/castor-pi-carrier:v1@sha256:aaaa",
        "python:pin",
    );
    let b = derive_task_identity(
        &spec,
        "a",
        "b",
        "substratum/castor-pi-carrier:v1@sha256:bbbb",
        "python:pin",
    );
    assert_ne!(a, b);
    assert!(a.0.starts_with("task-pack-"));
    assert!(a.1.starts_with("pack-"));
}

#[cfg(target_os = "macos")]
#[test]
fn mac_project_run_rejects_before_docker_or_model_access() {
    let root = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_castor"))
        .args(["run", "--project", "missing", "--task-spec", "missing.json"])
        .env("PATH", root.path())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("requires Linux"));
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
}
