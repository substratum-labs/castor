use super::{
    invalid,
    io::{open_root_dir, regular_file_at},
};
use std::collections::BTreeMap;
use std::fs;
use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const META_CAP: usize = 16 * 1024 * 1024;
const BLOB_CAP: usize = 32 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct TrackedEntry {
    pub path: PathBuf,
    pub mode: u32,
    pub object_id: String,
}
pub struct CleanTreeMetadata {
    pub commit: String,
    pub tracked_files: Vec<TrackedEntry>,
    pub root: PathBuf,
    pub git_dir: PathBuf,
}

fn kill_group(child: &mut Child) {
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.wait();
}

fn capped_read<R: Read>(reader: R, cap: usize) -> io::Result<Vec<u8>> {
    let mut out = Vec::new();
    reader.take((cap + 1) as u64).read_to_end(&mut out)?;
    if out.len() > cap {
        return Err(invalid("Git output cap exceeded"));
    }
    Ok(out)
}

pub(super) fn invoke(
    mut cmd: Command,
    input: Option<&[u8]>,
    out_cap: usize,
    err_cap: usize,
    deadline: Instant,
) -> io::Result<Vec<u8>> {
    let mut child = cmd
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()?;
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let out_thread = thread::spawn(move || capped_read(stdout, out_cap));
    let err_thread = thread::spawn(move || capped_read(stderr, err_cap));
    if let Some(input) = input {
        if let Some(mut stdin) = child.stdin.take() {
            stdin.write_all(input)?;
        }
    }
    let mut status = None;
    loop {
        if Instant::now() >= deadline {
            kill_group(&mut child);
            status = None;
            break;
        }
        if status.is_none() {
            status = child.try_wait()?;
        }
        if status.is_some() && out_thread.is_finished() && err_thread.is_finished() {
            break;
        }
        if status.is_none() && out_thread.is_finished() && err_thread.is_finished() {
            // A cap error closes the pipes; never wait unbounded for descendants.
            kill_group(&mut child);
            break;
        }
        thread::sleep(Duration::from_millis(5));
    }
    let out = out_thread
        .join()
        .map_err(|_| io::Error::other("Git stdout reader panicked"))??;
    let err = err_thread
        .join()
        .map_err(|_| io::Error::other("Git stderr reader panicked"))??;
    match status {
        Some(s) if s.success() => Ok(out),
        None => Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "Git operation deadline",
        )),
        _ => Err(invalid(format!(
            "Git plumbing failed: {}",
            String::from_utf8_lossy(&err)
        ))),
    }
}

fn git_command(root: &Path, git_dir: &Path) -> Command {
    let mut cmd = Command::new("/usr/bin/git");
    cmd.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .env("GIT_NO_REPLACE_OBJECTS", "1")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("GIT_NO_LAZY_FETCH", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .arg(format!("--work-tree={}", root.display()))
        .arg(format!("--git-dir={}", git_dir.display()))
        .arg("-c")
        .arg(format!("safe.directory={}", root.display()));
    if root != git_dir {
        cmd.arg("-c")
            .arg(format!("safe.directory={}", git_dir.display()));
    }
    cmd.args([
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.quotePath=false",
    ]);
    cmd
}

fn git(
    root: &Path,
    git_dir: &Path,
    args: &[&str],
    input: Option<&[u8]>,
    out_cap: usize,
    err_cap: usize,
    deadline: Instant,
) -> io::Result<Vec<u8>> {
    let mut cmd = git_command(root, git_dir);
    cmd.args(args);
    invoke(cmd, input, out_cap, err_cap, deadline)
}

fn resolved_git_dir(root: &Path) -> io::Result<PathBuf> {
    let dotgit = root.join(".git");
    let meta = fs::symlink_metadata(&dotgit)?;
    if meta.file_type().is_symlink() {
        return Err(invalid("symlink .git is unsupported"));
    }
    if meta.is_dir() {
        open_root_dir(&dotgit)?;
        return Ok(dotgit);
    }
    if !meta.is_file() {
        return Err(invalid("invalid .git entry"));
    }
    let text = String::from_utf8(nofollow_bytes(&dotgit, 4096)?)
        .map_err(|_| invalid("invalid gitdir file"))?;
    let link = text
        .trim()
        .strip_prefix("gitdir: ")
        .ok_or_else(|| invalid("invalid gitdir file"))?;
    let path = if Path::new(link).is_absolute() {
        PathBuf::from(link)
    } else {
        root.join(link)
    };
    let canonical = fs::canonicalize(path)?;
    open_root_dir(&canonical)?;
    Ok(canonical)
}

fn nofollow_bytes(path: &Path, cap: usize) -> io::Result<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.len() > cap as u64 {
        return Err(invalid("unsafe Git metadata file"));
    }
    capped_read(file, cap)
}

fn check_one_config(path: &Path) -> io::Result<()> {
    let config = String::from_utf8(nofollow_bytes(path, 1024 * 1024)?)
        .map_err(|_| invalid("non-UTF-8 Git config"))?;
    let mut section = String::new();
    for line in config.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            section = line.to_ascii_lowercase();
            continue;
        }
        let (key, value) = line.split_once('=').unwrap_or((line, ""));
        let key = key.trim().to_ascii_lowercase();
        let value = value
            .trim()
            .split(['#', ';'])
            .next()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        let enabled = matches!(value.as_str(), "" | "true" | "yes" | "on" | "1");
        if (section.starts_with("[extensions") && key == "partialclone")
            || (section.starts_with("[remote ") && key == "promisor" && enabled)
            || (section.starts_with("[core") && key == "sparsecheckout" && enabled)
            || (section.starts_with("[index") && key == "sparse" && enabled)
            || section.starts_with("[include")
        {
            return Err(invalid("unsupported partial or sparse Git repository"));
        }
    }
    Ok(())
}

fn check_config(git_dir: &Path) -> io::Result<()> {
    let common_file = git_dir.join("commondir");
    let common = if common_file.exists() {
        let value = String::from_utf8(nofollow_bytes(&common_file, 4096)?)
            .map_err(|_| invalid("invalid commondir"))?;
        fs::canonicalize(git_dir.join(value.trim()))?
    } else {
        git_dir.to_path_buf()
    };
    open_root_dir(&common)?;
    check_one_config(&common.join("config"))?;
    if common != git_dir {
        let worktree_config = git_dir.join("config.worktree");
        if worktree_config.exists() {
            check_one_config(&worktree_config)?;
        }
    }
    match open_root_dir(&common.join("objects/info")) {
        Ok(info) => {
            if let Some(alternate) = regular_file_at(&info, std::ffi::OsStr::new("alternates"))? {
                if alternate.metadata()?.len() > 0 {
                    return Err(invalid("unsupported Git alternates"));
                }
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => (),
        Err(error) => return Err(error),
    }
    Ok(())
}

fn parse_entries(bytes: &[u8], tree: bool) -> io::Result<BTreeMap<Vec<u8>, (u32, String)>> {
    let mut entries = BTreeMap::new();
    for entry in bytes.split(|b| *b == 0).filter(|v| !v.is_empty()) {
        let tab = entry
            .iter()
            .position(|b| *b == b'\t')
            .ok_or_else(|| invalid("malformed Git entry"))?;
        let (header, path) = entry.split_at(tab);
        let path = &path[1..];
        if path.is_empty()
            || path[0] == b'/'
            || path
                .split(|b| *b == b'/')
                .any(|p| p == b".." || p.is_empty())
        {
            return Err(invalid("unsafe Git path"));
        }
        let header = std::str::from_utf8(header).map_err(|_| invalid("invalid Git metadata"))?;
        let pieces: Vec<_> = header.split(' ').collect();
        let (mode, oid) = if tree {
            if pieces.len() != 3 {
                return Err(invalid("malformed tree"));
            }
            (pieces[0], pieces[2])
        } else {
            if pieces.len() != 3 || pieces[2] != "0" {
                return Err(invalid("unmerged Git index"));
            }
            (pieces[0], pieces[1])
        };
        let mode = u32::from_str_radix(mode, 8).map_err(|_| invalid("invalid Git mode"))?;
        if mode != 0o100644 && mode != 0o100755 {
            return Err(invalid("unsupported Git entry type"));
        }
        if entries
            .insert(path.to_vec(), (mode, oid.to_owned()))
            .is_some()
        {
            return Err(invalid("duplicate Git path"));
        }
    }
    Ok(entries)
}

pub fn inspect_clean_git_tree(
    project_dir: &Path,
    deadline: Instant,
) -> io::Result<CleanTreeMetadata> {
    let root_fd = open_root_dir(project_dir)?;
    let root = fs::canonicalize(project_dir)?;
    let reopened = open_root_dir(&root)?;
    if root_fd.metadata()?.ino() != reopened.metadata()?.ino()
        || root_fd.metadata()?.dev() != reopened.metadata()?.dev()
    {
        return Err(invalid("project root changed during resolution"));
    }
    let git_dir = resolved_git_dir(&root)?;
    check_config(&git_dir)?;
    let commit_bytes = git(
        &root,
        &git_dir,
        &["rev-parse", "HEAD"],
        None,
        META_CAP,
        META_CAP,
        deadline,
    )?;
    let commit = String::from_utf8(commit_bytes)
        .map_err(|_| invalid("invalid commit"))?
        .trim()
        .to_owned();
    if commit.len() != 40 && commit.len() != 64 {
        return Err(invalid("invalid commit ID"));
    }
    let tree = git(
        &root,
        &git_dir,
        &["ls-tree", "-rz", "--full-tree", "HEAD"],
        None,
        META_CAP,
        META_CAP,
        deadline,
    )?;
    let index = git(
        &root,
        &git_dir,
        &["ls-files", "--stage", "-z"],
        None,
        META_CAP,
        META_CAP,
        deadline,
    )?;
    let tree = parse_entries(&tree, true)?;
    let index = parse_entries(&index, false)?;
    if tree != index {
        return Err(invalid("Git index differs from HEAD"));
    }
    let others = git(
        &root,
        &git_dir,
        &["ls-files", "--others", "--exclude-standard", "-z"],
        None,
        META_CAP,
        META_CAP,
        deadline,
    )?;
    if !others.is_empty() {
        return Err(invalid("untracked project files"));
    }
    let tracked_files = tree
        .into_iter()
        .map(|(path, (mode, object_id))| {
            use std::os::unix::ffi::OsStringExt;
            TrackedEntry {
                path: PathBuf::from(std::ffi::OsString::from_vec(path)),
                mode,
                object_id,
            }
        })
        .collect();
    Ok(CleanTreeMetadata {
        commit,
        tracked_files,
        root,
        git_dir,
    })
}

pub fn blob(meta: &CleanTreeMetadata, oid: &str, deadline: Instant) -> io::Result<Vec<u8>> {
    let input = format!("{oid}\n");
    let out = git(
        &meta.root,
        &meta.git_dir,
        &["cat-file", "--batch"],
        Some(input.as_bytes()),
        BLOB_CAP + 4096 + 2,
        1024 * 1024,
        deadline,
    )?;
    let end = out
        .iter()
        .position(|b| *b == b'\n')
        .ok_or_else(|| invalid("missing blob header"))?;
    if end > 4096 {
        return Err(invalid("blob header cap exceeded"));
    }
    let header = std::str::from_utf8(&out[..end]).map_err(|_| invalid("bad blob header"))?;
    let fields: Vec<_> = header.split(' ').collect();
    if fields.len() != 3 || fields[1] != "blob" || fields[0] != oid {
        return Err(invalid("unexpected Git object"));
    }
    let size: usize = fields[2].parse().map_err(|_| invalid("bad blob size"))?;
    if size > BLOB_CAP || out.len() != end + 1 + size + 1 || out.last() != Some(&b'\n') {
        return Err(invalid("blob size mismatch or cap exceeded"));
    }
    Ok(out[end + 1..end + 1 + size].to_vec())
}
