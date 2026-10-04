//! Trusted Linux controller and Engine-namespace helper for developer mode.
#[cfg(any(test, target_os = "linux"))]
use castor_kernel::one_shot::image::{valid_digest, validate_base_image};
#[cfg(target_os = "linux")]
use serde_json::{json, Value};
#[cfg(target_os = "linux")]
use std::env;
#[cfg(target_os = "linux")]
use std::ffi::CString;
#[cfg(any(test, target_os = "linux"))]
use std::fs;
#[cfg(target_os = "linux")]
use std::fs::{File, OpenOptions};
use std::io;
#[cfg(target_os = "linux")]
use std::io::Write;
#[cfg(target_os = "linux")]
use std::os::unix::fs::{FileTypeExt, MetadataExt};
#[cfg(target_os = "linux")]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
#[cfg(any(test, target_os = "linux"))]
use std::path::Path;
#[cfg(target_os = "linux")]
use std::path::PathBuf;
use std::process::ExitCode;
#[cfg(target_os = "linux")]
use std::process::{Command, Stdio};
#[cfg(target_os = "linux")]
use std::thread;
#[cfg(target_os = "linux")]
use std::time::{Duration, Instant};

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(any(test, target_os = "linux"))]
fn validated_release_pins(
    carrier: Option<&str>,
    verifier: Option<&str>,
) -> io::Result<Option<(String, String)>> {
    match (carrier, verifier) {
        (None, None) => Ok(None),
        (Some(carrier), Some(verifier)) => {
            validate_base_image(carrier)?;
            if !valid_digest(verifier) {
                return Err(invalid("invalid release verifier image ID"));
            }
            Ok(Some((carrier.to_owned(), verifier.to_owned())))
        }
        _ => Err(invalid("incomplete controller release pins")),
    }
}

#[cfg(any(test, target_os = "linux"))]
fn private_scratch(parent: &Path, path: &Path, token: &str) -> io::Result<()> {
    if token.len() != 32
        || !token
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err(invalid("invalid scratch owner token"));
    }
    if !parent.is_absolute()
        || !path.is_absolute()
        || path != parent.join(format!("c-{}", &token[..12]))
        || fs::canonicalize(parent)? != parent
    {
        return Err(invalid(
            "scratch path is not the exact canonical owned child",
        ));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
pub fn engine_helper(raw: &str) -> io::Result<ExitCode> {
    let root = CString::new("/engine").unwrap();
    let slash = CString::new("/").unwrap();
    if unsafe { libc::chroot(root.as_ptr()) } != 0 {
        return Err(io::Error::other(format!(
            "Engine helper chroot /engine: {}",
            io::Error::last_os_error()
        )));
    }
    if unsafe { libc::chdir(slash.as_ptr()) } != 0 {
        return Err(io::Error::other(format!(
            "Engine helper chdir /: {}",
            io::Error::last_os_error()
        )));
    }
    let request: Value =
        serde_json::from_str(raw).map_err(|_| invalid("invalid helper request"))?;
    let mode = request
        .get("operation")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("missing helper operation"))?;
    if mode == "canonical" {
        let paths = request
            .get("paths")
            .and_then(Value::as_array)
            .ok_or_else(|| invalid("missing canonical paths"))?;
        if paths.is_empty() || paths.len() > 8 {
            return Err(invalid("invalid canonical path count"));
        }
        let resolved: Vec<String> = paths
            .iter()
            .map(|value| {
                let path = value
                    .as_str()
                    .ok_or_else(|| invalid("invalid canonical path"))?;
                if !Path::new(path).is_absolute() {
                    return Err(invalid("canonical path must be absolute"));
                }
                fs::canonicalize(path)
                    .map_err(|error| io::Error::other(format!("Engine canonical {path}: {error}")))?
                    .into_os_string()
                    .into_string()
                    .map_err(|_| invalid("non-UTF8 Engine path"))
            })
            .collect::<io::Result<_>>()?;
        println!(
            "{}",
            serde_json::to_string(&resolved).map_err(io::Error::other)?
        );
        return Ok(ExitCode::SUCCESS);
    }
    if mode == "identity" {
        let state = request
            .get("state")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing identity state"))?;
        if !Path::new(state).is_absolute() || fs::canonicalize(state)? != Path::new(state) {
            return Err(invalid("identity state is not canonical"));
        }
        let state_meta = fs::metadata(state)?;
        let socket_meta = fs::metadata("/var/run/docker.sock")?;
        if !state_meta.is_dir() || !socket_meta.file_type().is_socket() {
            return Err(invalid("Engine state or Docker socket has wrong type"));
        }
        println!(
            "{}",
            json!({
                "state_uid":state_meta.uid(), "state_gid":state_meta.gid(),
                "socket_uid":socket_meta.uid(), "socket_gid":socket_meta.gid(),
                "socket_mode":socket_meta.mode() & 0o777,
            })
        );
        return Ok(ExitCode::SUCCESS);
    }
    let parent = PathBuf::from(
        request
            .get("parent")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing scratch parent"))?,
    );
    let path = PathBuf::from(
        request
            .get("scratch")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing scratch path"))?,
    );
    let token = request
        .get("token")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("missing owner token"))?;
    private_scratch(&parent, &path, token)?;
    match mode {
        "allocate" => {
            let uid = request
                .get("uid")
                .and_then(Value::as_u64)
                .and_then(|v| u32::try_from(v).ok())
                .ok_or_else(|| invalid("missing scratch UID"))?;
            let gid = request
                .get("gid")
                .and_then(Value::as_u64)
                .and_then(|v| u32::try_from(v).ok())
                .ok_or_else(|| invalid("missing scratch GID"))?;
            fs::create_dir(&path)?; // exclusive: never adopt another user's directory
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
            let marker = path.join(".launcher-owner");
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(marker)?;
            file.write_all(token.as_bytes())?;
            file.sync_all()?;
            // Keep the already-open directory handle: after chown, cap-free
            // root cannot traverse a different UID's 0700 scratch path.
            let directory = File::open(&path)?;
            let c_path = CString::new(path.as_os_str().as_encoded_bytes())
                .map_err(|_| invalid("invalid scratch path"))?;
            if unsafe { libc::chown(c_path.as_ptr(), uid, gid) } != 0 {
                return Err(io::Error::last_os_error());
            }
            directory.sync_all()?;
            File::open(&parent)?.sync_all()?;
        }
        "remove" => {
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.is_dir() || metadata.file_type().is_symlink() {
                return Err(invalid("private scratch is not a directory"));
            }
            let marker = path.join(".launcher-owner");
            let owner = fs::read_to_string(&marker)?;
            if owner != token {
                return Err(invalid("scratch owner mismatch"));
            }
            // The Engine helper alone has write access to the exact 0700 root.
            fs::remove_dir_all(&path)?;
            File::open(&parent)?.sync_all()?;
        }
        _ => return Err(invalid("unknown helper operation")),
    }
    println!("{}", json!({"scratch":path,"exists":path.exists()}));
    Ok(ExitCode::SUCCESS)
}

#[cfg(not(target_os = "linux"))]
pub fn engine_helper(_raw: &str) -> io::Result<ExitCode> {
    Err(invalid("Engine helper requires Linux"))
}

#[cfg(target_os = "linux")]
pub fn run_controller() -> io::Result<ExitCode> {
    let state = Path::new("/state/controller");
    let bridge = state.join("bridge");
    let socket = PathBuf::from(
        env::var("CASTOR_CONTROLLER_MODEL_SOCKET")
            .map_err(|_| invalid("missing controller model socket"))?,
    );
    let scratch =
        PathBuf::from(env::var("TMPDIR").map_err(|_| invalid("missing controller scratch"))?);
    let spec = PathBuf::from(
        env::var("CASTOR_CONTROLLER_TASK_SPEC")
            .map_err(|_| invalid("missing controller task spec"))?,
    );
    if !socket.is_absolute() || !socket.starts_with(&scratch) || !spec.starts_with("/spec") {
        return Err(invalid("invalid controller mount layout"));
    }
    for (name, path, directory) in [
        ("bridge", bridge.as_path(), true),
        ("project", Path::new("/project"), true),
        ("task spec", spec.as_path(), false),
        ("scratch", scratch.as_path(), true),
    ] {
        let meta = fs::metadata(path).map_err(|e| {
            io::Error::new(
                e.kind(),
                format!("controller {name} mount {}: {e}", path.display()),
            )
        })?;
        if meta.is_dir() != directory {
            return Err(invalid("invalid controller mount layout"));
        }
    }
    let timeout_ms: u64 = env::var("CASTOR_CONTROLLER_TIMEOUT_MS")
        .map_err(|_| invalid("missing controller timeout"))?
        .parse()
        .map_err(|_| invalid("invalid controller timeout"))?;
    if !(1..=300_000).contains(&timeout_ms) {
        return Err(invalid("controller timeout outside bound"));
    }
    let stdout = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(state.join("bridge.stdout"))?;
    let stderr = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(state.join("bridge.stderr"))?;
    let mut bridge_child = Command::new("node")
        .arg("/opt/castor/file_exchange_bridge.mjs")
        .arg(&socket)
        .arg(&bridge)
        .arg(timeout_ms.to_string())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()?;
    let ready = Instant::now() + Duration::from_secs(5);
    while !socket.exists() {
        if bridge_child.try_wait()?.is_some() || Instant::now() >= ready {
            let _ = bridge_child.kill();
            let _ = bridge_child.wait();
            return Err(io::Error::other("controller model bridge failed to start"));
        }
        thread::sleep(Duration::from_millis(10));
    }
    let release_pins = validated_release_pins(
        env::var("CASTOR_RELEASE_CARRIER_REF")
            .ok()
            .filter(|value| !value.is_empty())
            .as_deref(),
        env::var("CASTOR_RELEASE_VERIFIER_ID")
            .ok()
            .filter(|value| !value.is_empty())
            .as_deref(),
    )?;
    let layout_digest = env::var("CASTOR_CONTROLLER_CARRIER_OCI_DIGEST").ok();
    if layout_digest.is_some() != release_pins.is_some() {
        return Err(invalid(
            "controller carrier OCI layout and release pins must agree",
        ));
    }
    if let Some(digest) = &layout_digest {
        if !valid_digest(digest) || !fs::metadata("/carrier-oci")?.is_dir() {
            return Err(invalid("invalid controller carrier OCI layout"));
        }
    }
    let mut native = Command::new("/usr/local/bin/castor");
    native
        .args(["run", "--project", "/project", "--task-spec"])
        .arg(&spec)
        .env("CASTOR_STATE_ROOT", "/state")
        .env("CASTOR_MODEL_SOCKET", &socket)
        .env("TMPDIR", &scratch);
    if let Some((carrier, verifier)) = release_pins {
        native
            .env("CASTOR_CONTROLLER_CARRIER_REF", carrier)
            .env("CASTOR_CONTROLLER_VERIFIER_ID", verifier);
    }
    if let Some(digest) = layout_digest {
        native
            .env("CASTOR_CARRIER_OCI_LAYOUT", "/carrier-oci")
            .env("CASTOR_CARRIER_OCI_DIGEST", digest);
    }
    let outcome = native.status();
    let _ = bridge_child.kill();
    let _ = bridge_child.wait();
    let result = outcome?;
    fs::write(
        state.join("native-exit.json"),
        serde_json::to_vec(&json!({"exit_code":result.code()})).map_err(io::Error::other)?,
    )?;
    Ok(ExitCode::from(
        result.code().unwrap_or(2).clamp(0, 255) as u8
    ))
}

#[cfg(not(target_os = "linux"))]
pub fn run_controller() -> io::Result<ExitCode> {
    Err(invalid("trusted controller requires Linux"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn controller_release_pins_reject_incomplete_or_unpinned_values() {
        let id = format!("sha256:{}", "a".repeat(64));
        let carrier = format!("substratum/castor-pi-carrier:one-shot-0.1.0@{id}");
        assert!(validated_release_pins(Some(&carrier), Some(&id))
            .unwrap()
            .is_some());
        assert!(validated_release_pins(Some(&carrier), None).is_err());
        assert!(validated_release_pins(
            Some("substratum/castor-pi-carrier:one-shot-0.1.0"),
            Some(&id)
        )
        .is_err());
    }

    #[test]
    fn private_scratch_requires_exact_canonical_child_and_owner() {
        let root = tempfile::tempdir().unwrap();
        let parent = fs::canonicalize(root.path()).unwrap();
        let token = "0123456789abcdef0123456789abcdef";
        assert!(private_scratch(&parent, &parent.join("c-0123456789ab"), token).is_ok());
        assert!(private_scratch(&parent, &parent.join("c-0123456789ac"), token).is_err());
        assert!(private_scratch(&parent, &parent.join("c-0123456789ab"), "short").is_err());
    }
}
