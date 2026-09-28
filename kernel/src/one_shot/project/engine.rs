use super::archive::build_deterministic_tar;
use super::git::{inspect_clean_git_tree, invoke};
use super::invalid;
#[cfg(target_os = "linux")]
use super::io::create_root_dir;
use super::io::open_root_dir;
use super::receipt::PackReceipt;
use super::spec::{derive_task_identity, parse_and_validate_spec};
use sha2::{Digest, Sha256};
use std::ffi::CString;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

pub const VERIFIER_PIN: &str =
    "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea";
const CARRIER_TAG: &str = "substratum/castor-pi-carrier:v1";

fn image_id(reference: &str, deadline: Instant) -> io::Result<String> {
    let mut cmd = Command::new("docker");
    cmd.args(["image", "inspect", "--format", "{{.Id}}", reference]);
    let output = invoke(cmd, None, 4096, 4096, deadline).map_err(|_| invalid(format!("missing local image {reference}; install Castor runtime images (carrier: docker build -t {CARRIER_TAG} <CASTOR_DIR>/kernel/carrier/pi)")))?;
    let id = String::from_utf8(output)
        .map_err(|_| invalid("invalid Docker image ID"))?
        .trim()
        .to_owned();
    if id.len() != 71
        || !id.starts_with("sha256:")
        || !id[7..]
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err(invalid("invalid local Docker image ID"));
    }
    Ok(id)
}

fn same_or_inside(project: &Path, output: &Path) -> io::Result<bool> {
    let absolute = if output.is_absolute() {
        output.to_path_buf()
    } else {
        std::env::current_dir()?.join(output)
    };
    let mut clean = PathBuf::new();
    for part in absolute.components() {
        match part {
            std::path::Component::RootDir => clean.push("/"),
            std::path::Component::Normal(name) => clean.push(name),
            std::path::Component::CurDir => (),
            std::path::Component::ParentDir => {
                clean.pop();
            }
            _ => return Err(invalid("invalid output path")),
        }
    }
    Ok(clean.starts_with(project))
}

fn existing_output(
    parent_fd: &File,
    out: &Path,
    expected: &[(String, Vec<u8>)],
) -> io::Result<bool> {
    let name = out
        .file_name()
        .ok_or_else(|| invalid("invalid output directory"))?;
    let c = CString::new(name.as_bytes()).map_err(|_| invalid("NUL in output"))?;
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    let rc = unsafe {
        libc::fstatat(
            parent_fd.as_raw_fd(),
            c.as_ptr(),
            &mut st,
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if rc < 0 {
        let err = io::Error::last_os_error();
        if err.kind() == io::ErrorKind::NotFound {
            return Ok(false);
        }
        return Err(err);
    }
    if st.st_mode & libc::S_IFMT != libc::S_IFDIR {
        return Err(invalid("output exists and is not a directory"));
    }
    let dir_fd = unsafe {
        libc::openat(
            parent_fd.as_raw_fd(),
            c.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if dir_fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let dir = unsafe { std::os::fd::OwnedFd::from_raw_fd(dir_fd) };
    use std::os::fd::FromRawFd;
    let scan_fd = unsafe { libc::dup(dir.as_raw_fd()) };
    if scan_fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let stream = unsafe { libc::fdopendir(scan_fd) };
    if stream.is_null() {
        unsafe {
            libc::close(scan_fd);
        }
        return Err(io::Error::last_os_error());
    }
    let mut names = Vec::new();
    loop {
        let entry = unsafe { libc::readdir(stream) };
        if entry.is_null() {
            break;
        }
        let name = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
        if name != b"." && name != b".." {
            names.push(name.to_vec());
        }
    }
    unsafe {
        libc::closedir(stream);
    }
    names.sort();
    if names
        != [
            b"manifest.json".to_vec(),
            b"pack-receipt.json".to_vec(),
            b"workspace.tar".to_vec(),
        ]
    {
        return Err(invalid("existing output is not the exact pack bundle"));
    }
    for (name, bytes) in expected {
        let c = CString::new(name.as_bytes()).unwrap();
        let fd = unsafe {
            libc::openat(
                dir.as_raw_fd(),
                c.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(invalid("existing bundle file missing or unsafe"));
        }
        let file = unsafe { File::from_raw_fd(fd) };
        use std::os::unix::fs::MetadataExt;
        let meta = file.metadata()?;
        if !meta.is_file() || meta.nlink() != 1 || meta.len() != bytes.len() as u64 {
            return Err(invalid("existing bundle file differs"));
        }
        let mut actual = Vec::new();
        file.take((bytes.len() + 1) as u64)
            .read_to_end(&mut actual)?;
        if &actual != bytes {
            return Err(invalid("existing bundle bytes differ"));
        }
    }
    Ok(true)
}

fn exclusive_rename(
    from_parent: &File,
    from: &Path,
    to_parent: &File,
    to: &Path,
) -> io::Result<()> {
    let from = CString::new(from.file_name().unwrap().as_bytes())
        .map_err(|_| invalid("invalid staging name"))?;
    let to = CString::new(to.file_name().unwrap().as_bytes())
        .map_err(|_| invalid("invalid output name"))?;
    #[cfg(target_os = "linux")]
    let rc = unsafe {
        libc::renameat2(
            from_parent.as_raw_fd(),
            from.as_ptr(),
            to_parent.as_raw_fd(),
            to.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    #[cfg(target_os = "macos")]
    let rc = unsafe {
        libc::renameatx_np(
            from_parent.as_raw_fd(),
            from.as_ptr(),
            to_parent.as_raw_fd(),
            to.as_ptr(),
            libc::RENAME_EXCL,
        )
    };
    if rc < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub fn pack_project(
    project_path: &Path,
    spec_path: &Path,
    out_dir: &Path,
) -> io::Result<PackReceipt> {
    let deadline = Instant::now() + Duration::from_secs(60);
    let project_fd = open_root_dir(project_path)?;
    let project = fs::canonicalize(project_path)?;
    match fs::symlink_metadata(project.join(".castor_verification_assets")) {
        Ok(_) => return Err(invalid("source collides with verification asset prefix")),
        Err(e) if e.kind() == io::ErrorKind::NotFound => (),
        Err(e) => return Err(e),
    }
    if same_or_inside(&project, out_dir)? {
        return Err(invalid("output path cannot reside inside input project"));
    }
    let spec = parse_and_validate_spec(spec_path)?;
    let tree = inspect_clean_git_tree(&project, deadline)?;
    let parent = out_dir
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let parent_fd = open_root_dir(parent)?;
    let parent_path = fs::canonicalize(parent)?;
    let output = parent_path.join(
        out_dir
            .file_name()
            .ok_or_else(|| invalid("invalid output directory"))?,
    );
    if same_or_inside(&project, &output)? {
        return Err(invalid("output path cannot reside inside input project"));
    }
    let carrier = format!("{CARRIER_TAG}@{}", image_id(CARRIER_TAG, deadline)?);
    let prefix = format!(
        "{}.tmp-pack-",
        output.file_name().unwrap().to_string_lossy()
    );
    let staging = tempfile::Builder::new()
        .prefix(&prefix)
        .tempdir_in(&parent_path)?;
    let tar_path = staging.path().join("workspace.tar");
    let spec_dir = spec_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let (snapshot, archived_entries, excluded_entries) =
        build_deterministic_tar(&project_fd, &tree, &spec, spec_dir, &tar_path, deadline)?;
    if Instant::now() >= deadline {
        return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
    }
    let (derived_id, derived_key) =
        derive_task_identity(&spec, &tree.commit, &snapshot, &carrier, VERIFIER_PIN);
    let task_id = spec.task_id.unwrap_or(derived_id);
    let idempotency_key = spec.idempotency_key.unwrap_or(derived_key);
    let manifest = serde_json::json!({
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "carrier_base_image": carrier,
        "workspace_snapshot_path": "workspace.tar",
        "workspace_snapshot_sha256": snapshot,
        "task_prompt": spec.task_prompt,
        "verification_command": spec.verification_command,
        "verification_timeout_seconds": spec.verification_timeout_seconds.unwrap_or(300)
    });
    let receipt = PackReceipt {
        schema_version: 1,
        source_commit: tree.commit,
        task_id,
        idempotency_key,
        carrier_base_image: carrier,
        verifier_image_pin: VERIFIER_PIN.into(),
        workspace_snapshot_sha256: snapshot,
        excluded_entries,
        archived_entries,
    };
    let manifest_bytes = serde_json::to_vec(&manifest).map_err(io::Error::other)?;
    let receipt_bytes = serde_json::to_vec(&receipt).map_err(io::Error::other)?;
    fs::write(staging.path().join("manifest.json"), &manifest_bytes)?;
    fs::write(staging.path().join("pack-receipt.json"), &receipt_bytes)?;
    let tar_bytes = fs::read(&tar_path)?;
    if Instant::now() >= deadline {
        return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
    }
    if format!("{:x}", Sha256::digest(&tar_bytes)) != receipt.workspace_snapshot_sha256 {
        return Err(invalid("staged tar digest changed"));
    }
    let expected = [
        ("manifest.json".into(), manifest_bytes),
        ("workspace.tar".into(), tar_bytes),
        ("pack-receipt.json".into(), receipt_bytes),
    ];
    if !existing_output(&parent_fd, &output, &expected)? {
        exclusive_rename(&parent_fd, staging.path(), &parent_fd, &output)?;
    }
    Ok(receipt)
}

pub fn run_project(project_path: &Path, spec_path: &Path) -> io::Result<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        let _ = (project_path, spec_path);
        Err(invalid("run --project requires Linux because model Unix sockets cannot cross the macOS container kernel"))
    }
    #[cfg(target_os = "linux")]
    {
        let state_root = std::env::var_os("CASTOR_STATE_ROOT")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".castor/state")))
            .ok_or_else(|| invalid("missing HOME or CASTOR_STATE_ROOT"))?;
        create_root_dir(&state_root.join("pack"))?;
        let verifier = image_id(VERIFIER_PIN, Instant::now() + Duration::from_secs(60))?;
        if !verifier.starts_with("sha256:") {
            return Err(invalid("invalid verifier image"));
        }
        let pack_root = state_root.join("pack");
        let stage_root = tempfile::Builder::new()
            .prefix(".run-pack-")
            .tempdir_in(&pack_root)?;
        let provisional = stage_root.path().join("bundle");
        let receipt = pack_project(project_path, spec_path, &provisional)?;
        let final_out = pack_root.join(&receipt.task_id);
        let parent_fd = open_root_dir(&pack_root)?;
        let files = ["manifest.json", "workspace.tar", "pack-receipt.json"];
        let expected = files
            .iter()
            .map(|name| Ok(((*name).to_owned(), fs::read(provisional.join(name))?)))
            .collect::<io::Result<Vec<_>>>()?;
        if !existing_output(&parent_fd, &final_out, &expected)? {
            let stage_fd = open_root_dir(stage_root.path())?;
            exclusive_rename(&stage_fd, &provisional, &parent_fd, &final_out)?;
        }
        Ok(final_out.join("manifest.json"))
    }
}
