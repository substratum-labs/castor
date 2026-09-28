use super::git::{blob, CleanTreeMetadata};
use super::invalid;
use super::io::{directory_names, open_child_dir, read_bounded};
use super::receipt::{ArchivedEntry, ExcludedEntry};
use super::spec::TaskSpec;
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::time::Instant;

const BLOB_CAP: usize = 32 * 1024 * 1024;
const TOTAL_CAP: usize = 256 * 1024 * 1024;
const FILE_CAP: usize = 10_000;

fn excluded(path: &str, spec: &TaskSpec) -> Option<&'static str> {
    if let Some(custom) = &spec.exclude_paths {
        for pattern in custom {
            if let Some(dir) = pattern.strip_suffix('/') {
                if path.starts_with(&format!("{dir}/")) {
                    return Some("spec-exclusion");
                }
            } else if path == pattern {
                return Some("spec-exclusion");
            }
        }
    }
    let parts: Vec<_> = path.split('/').collect();
    if parts.iter().any(|p| matches!(*p, ".git" | ".castor"))
        || parts[..parts.len().saturating_sub(1)]
            .iter()
            .any(|p| matches!(*p, "node_modules" | "__pycache__" | ".venv" | "target"))
    {
        return Some("default-exclusion");
    }
    let name = parts.last().copied().unwrap_or("");
    if name.ends_with(".pem")
        || name.ends_with(".key")
        || name == ".env"
        || name.starts_with(".env.")
        || name == "id_rsa"
        || name == "id_ed25519"
    {
        return Some("default-exclusion");
    }
    None
}

struct Member {
    path: String,
    bytes: Vec<u8>,
    mode: u32,
    origin: &'static str,
}

pub struct TarBundle {
    pub digest: String,
    pub bytes: Vec<u8>,
    pub archived: Vec<ArchivedEntry>,
    pub excluded: Vec<ExcludedEntry>,
}

fn collect_assets(
    dir: &File,
    relative: &Path,
    spec: &TaskSpec,
    members: &mut Vec<Member>,
    excluded_entries: &mut Vec<ExcludedEntry>,
    deadline: Instant,
) -> io::Result<()> {
    if Instant::now() >= deadline {
        return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
    }
    for name in directory_names(dir)? {
        let rel = relative.join(&name);
        let path = rel
            .to_str()
            .ok_or_else(|| invalid("non-UTF-8 asset path"))?;
        if let Ok(child) = open_child_dir(dir, &name) {
            collect_assets(&child, &rel, spec, members, excluded_entries, deadline)?;
        } else {
            let (bytes, mode) = read_bounded(dir, Path::new(&name), BLOB_CAP)?;
            if mode & u32::from(libc::S_IXUSR) != 0 {
                return Err(invalid("executable verification asset"));
            }
            let archive_path = format!(".castor_verification_assets/{path}");
            if let Some(reason) = excluded(path, spec) {
                excluded_entries.push(ExcludedEntry {
                    path: archive_path,
                    reason: reason.into(),
                });
            } else {
                members.push(Member {
                    path: archive_path,
                    bytes,
                    mode: 0o644,
                    origin: "asset",
                });
            }
        }
    }
    Ok(())
}

pub fn build_deterministic_tar(
    project_fd: &File,
    tree: &CleanTreeMetadata,
    spec: &TaskSpec,
    spec_dir: &File,
    out_tar_file: File,
    deadline: Instant,
) -> io::Result<TarBundle> {
    let mut members = Vec::new();
    let mut excluded_entries = Vec::new();
    for tracked in &tree.tracked_files {
        if Instant::now() >= deadline {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
        }
        let path = tracked
            .path
            .to_str()
            .ok_or_else(|| invalid("non-UTF-8 Git path"))?;
        if path == ".castor_verification_assets" || path.starts_with(".castor_verification_assets/")
        {
            return Err(invalid("source collides with verification asset prefix"));
        }
        let (bytes, mode) = read_bounded(project_fd, &tracked.path, BLOB_CAP)?;
        let actual_mode = if mode & 0o111 != 0 {
            0o100755
        } else {
            0o100644
        };
        if actual_mode != tracked.mode {
            return Err(invalid(format!("source mode differs from Git: {path}")));
        }
        if bytes != blob(tree, &tracked.object_id, deadline)? {
            return Err(invalid(format!("dirty tracked file: {path}")));
        }
        if let Some(reason) = excluded(path, spec) {
            excluded_entries.push(ExcludedEntry {
                path: path.into(),
                reason: reason.into(),
            });
        } else {
            members.push(Member {
                path: path.into(),
                bytes,
                mode: actual_mode & 0o777,
                origin: "source",
            });
        }
    }
    if let Some(assets) = &spec.verification_assets {
        let mut assets_fd = spec_dir.try_clone()?;
        for part in assets.components() {
            let std::path::Component::Normal(name) = part else {
                return Err(invalid("invalid verification asset path"));
            };
            assets_fd = open_child_dir(&assets_fd, name)?;
        }
        collect_assets(
            &assets_fd,
            Path::new(""),
            spec,
            &mut members,
            &mut excluded_entries,
            deadline,
        )?;
    }
    members.sort_by(|a, b| a.path.as_bytes().cmp(b.path.as_bytes()));
    excluded_entries.sort_by(|a, b| a.path.as_bytes().cmp(b.path.as_bytes()));
    if members.len() > FILE_CAP {
        return Err(invalid("too many archive members"));
    }
    let mut total = 0usize;
    let mut entries = Vec::new();
    let mut tar = tar::Builder::new(out_tar_file);
    for member in members {
        if Instant::now() >= deadline {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
        }
        total = total
            .checked_add(member.bytes.len())
            .ok_or_else(|| invalid("archive size overflow"))?;
        if total > TOTAL_CAP {
            return Err(invalid("archive byte cap exceeded"));
        }
        let mut header = tar::Header::new_ustar();
        header.set_size(member.bytes.len() as u64);
        header.set_mode(member.mode);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_username("")?;
        header.set_groupname("")?;
        header.set_cksum();
        tar.append_data(&mut header, &member.path, member.bytes.as_slice())?;
        entries.push(ArchivedEntry {
            path: member.path,
            bytes: member.bytes.len(),
            sha256: format!("{:x}", Sha256::digest(&member.bytes)),
            mode: member.mode,
            origin: member.origin.into(),
        });
    }
    let mut file = tar.into_inner()?;
    file.flush()?;
    file.sync_all()?;
    if Instant::now() >= deadline {
        return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
    }
    file.seek(SeekFrom::Start(0))?;
    let mut bytes = Vec::new();
    file.take((TOTAL_CAP + FILE_CAP * 1024 + 4096) as u64)
        .read_to_end(&mut bytes)?;
    let digest = format!("{:x}", Sha256::digest(&bytes));
    Ok(TarBundle {
        digest,
        bytes,
        archived: entries,
        excluded: excluded_entries,
    })
}
