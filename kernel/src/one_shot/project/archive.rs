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
const DIR_CAP: usize = 10_000;
const ENTRY_CAP: usize = 50_000;
const DEPTH_CAP: usize = 64;

struct ArchiveBudget {
    bytes: usize,
    files: usize,
    dirs: usize,
    entries: usize,
    byte_cap: usize,
    file_cap: usize,
    dir_cap: usize,
    entry_cap: usize,
    depth_cap: usize,
}

struct AssetScanContext<'a> {
    spec: &'a TaskSpec,
    deadline: Instant,
}

impl ArchiveBudget {
    fn standard() -> Self {
        Self {
            bytes: 0,
            files: 0,
            dirs: 0,
            entries: 0,
            byte_cap: TOTAL_CAP,
            file_cap: FILE_CAP,
            dir_cap: DIR_CAP,
            entry_cap: ENTRY_CAP,
            depth_cap: DEPTH_CAP,
        }
    }

    #[cfg(test)]
    fn testing(
        byte_cap: usize,
        file_cap: usize,
        dir_cap: usize,
        entry_cap: usize,
        depth_cap: usize,
    ) -> Self {
        Self {
            byte_cap,
            file_cap,
            dir_cap,
            entry_cap,
            depth_cap,
            ..Self::standard()
        }
    }

    fn file_read_cap(&self) -> io::Result<usize> {
        if self.files >= self.file_cap {
            return Err(invalid("too many archive members"));
        }
        Ok(BLOB_CAP.min(self.byte_cap.saturating_sub(self.bytes)))
    }

    fn add_file(&mut self, size: usize) -> io::Result<()> {
        self.bytes = self
            .bytes
            .checked_add(size)
            .ok_or_else(|| invalid("archive size overflow"))?;
        if self.bytes > self.byte_cap {
            return Err(invalid("archive byte cap exceeded"));
        }
        self.files += 1;
        Ok(())
    }
}

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
    context: &AssetScanContext<'_>,
    members: &mut Vec<Member>,
    excluded_entries: &mut Vec<ExcludedEntry>,
    budget: &mut ArchiveBudget,
    depth: usize,
) -> io::Result<()> {
    if Instant::now() >= context.deadline {
        return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
    }
    if depth > budget.depth_cap || budget.dirs >= budget.dir_cap {
        return Err(invalid("asset traversal directory/depth cap exceeded"));
    }
    budget.dirs += 1;
    for name in directory_names(
        dir,
        budget.entry_cap.saturating_sub(budget.entries),
        context.deadline,
    )? {
        if Instant::now() >= context.deadline {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
        }
        budget.entries += 1;
        let rel = relative.join(&name);
        let path = rel
            .to_str()
            .ok_or_else(|| invalid("non-UTF-8 asset path"))?;
        if let Ok(child) = open_child_dir(dir, &name) {
            collect_assets(
                &child,
                &rel,
                context,
                members,
                excluded_entries,
                budget,
                depth + 1,
            )?;
        } else {
            let exclusion = excluded(path, context.spec);
            let cap = if exclusion.is_some() {
                BLOB_CAP
            } else {
                budget.file_read_cap()?
            };
            let (bytes, mode) = read_bounded(dir, Path::new(&name), cap)?;
            if mode & u32::from(libc::S_IXUSR) != 0 {
                return Err(invalid("executable verification asset"));
            }
            let archive_path = format!(".castor_verification_assets/{path}");
            if let Some(reason) = exclusion {
                excluded_entries.push(ExcludedEntry {
                    path: archive_path,
                    reason: reason.into(),
                });
            } else {
                budget.add_file(bytes.len())?;
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
    let mut budget = ArchiveBudget::standard();
    for tracked in &tree.tracked_files {
        if Instant::now() >= deadline {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
        }
        if budget.entries >= budget.entry_cap {
            return Err(invalid("pack traversal entry cap exceeded"));
        }
        budget.entries += 1;
        let path = tracked
            .path
            .to_str()
            .ok_or_else(|| invalid("non-UTF-8 Git path"))?;
        if path == ".castor_verification_assets" || path.starts_with(".castor_verification_assets/")
        {
            return Err(invalid("source collides with verification asset prefix"));
        }
        let exclusion = excluded(path, spec);
        let cap = if exclusion.is_some() {
            BLOB_CAP
        } else {
            budget.file_read_cap()?
        };
        let (bytes, mode) = read_bounded(project_fd, &tracked.path, cap)?;
        let actual_mode = if mode & u32::from(libc::S_IXUSR) != 0 {
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
        if let Some(reason) = exclusion {
            excluded_entries.push(ExcludedEntry {
                path: path.into(),
                reason: reason.into(),
            });
        } else {
            budget.add_file(bytes.len())?;
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
            &AssetScanContext { spec, deadline },
            &mut members,
            &mut excluded_entries,
            &mut budget,
            0,
        )?;
    }
    members.sort_by(|a, b| a.path.as_bytes().cmp(b.path.as_bytes()));
    excluded_entries.sort_by(|a, b| a.path.as_bytes().cmp(b.path.as_bytes()));
    let mut entries = Vec::new();
    let mut tar = tar::Builder::new(out_tar_file);
    for member in members {
        if Instant::now() >= deadline {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "pack deadline"));
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::one_shot::project::io::open_root_dir;
    use std::fs;

    #[test]
    fn asset_collection_enforces_small_byte_member_and_depth_budgets() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("a"), b"a").unwrap();
        fs::write(root.path().join("b"), b"b").unwrap();
        let dir = open_root_dir(root.path()).unwrap();
        let spec = serde_json::from_value(serde_json::json!({
            "schema_version": 1,
            "task_prompt": "x",
            "verification_command": ["true"]
        }))
        .unwrap();
        let mut members = Vec::new();
        let mut excluded = Vec::new();
        let context = AssetScanContext {
            spec: &spec,
            deadline: Instant::now() + std::time::Duration::from_secs(2),
        };
        let mut budget = ArchiveBudget::testing(1, 2, 2, 2, 2);
        let error = collect_assets(
            &dir,
            Path::new(""),
            &context,
            &mut members,
            &mut excluded,
            &mut budget,
            0,
        )
        .unwrap_err();
        assert!(error.to_string().contains("cap"));
        assert_eq!(members.len(), 1);
        let mut budget = ArchiveBudget::testing(2, 1, 2, 2, 2);
        members.clear();
        let error = collect_assets(
            &dir,
            Path::new(""),
            &context,
            &mut members,
            &mut excluded,
            &mut budget,
            0,
        )
        .unwrap_err();
        assert!(error.to_string().contains("archive members"));
        assert_eq!(members.len(), 1);
        let mut budget = ArchiveBudget::testing(2, 2, 2, 1, 2);
        members.clear();
        let error = collect_assets(
            &dir,
            Path::new(""),
            &context,
            &mut members,
            &mut excluded,
            &mut budget,
            0,
        )
        .unwrap_err();
        assert!(error.to_string().contains("entry cap"));
        fs::create_dir(root.path().join("nested")).unwrap();
        let mut budget = ArchiveBudget::testing(2, 2, 3, 3, 0);
        members.clear();
        let error = collect_assets(
            &dir,
            Path::new(""),
            &context,
            &mut members,
            &mut excluded,
            &mut budget,
            0,
        )
        .unwrap_err();
        assert!(error.to_string().contains("directory/depth cap"));
    }

    #[test]
    fn asset_directory_scan_checks_deadline_before_reading_names() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("a"), b"a").unwrap();
        let dir = open_root_dir(root.path()).unwrap();
        let deadline = Instant::now() - std::time::Duration::from_millis(1);
        let error = directory_names(&dir, 1, deadline).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }
}
