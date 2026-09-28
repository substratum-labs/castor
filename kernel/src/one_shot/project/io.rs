use super::invalid;
use std::ffi::{CString, OsStr};
use std::fs::File;
use std::io::{self, Read};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path};

fn openat(parent: &File, name: &OsStr, flags: i32) -> io::Result<File> {
    let name = CString::new(name.as_bytes()).map_err(|_| invalid("NUL in path"))?;
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

pub fn open_root_dir(path: &Path) -> io::Result<File> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut root = File::open("/")?;
    for part in absolute.components() {
        match part {
            Component::RootDir | Component::CurDir => (),
            Component::Normal(name) => {
                root = openat(
                    &root,
                    name,
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )?
            }
            Component::ParentDir => {
                root = openat(
                    &root,
                    OsStr::new(".."),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )?;
            }
            _ => return Err(invalid("invalid root path")),
        }
    }
    Ok(root)
}

pub fn create_root_dir(path: &Path) -> io::Result<File> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut root = File::open("/")?;
    for part in absolute.components() {
        match part {
            Component::RootDir | Component::CurDir => (),
            Component::Normal(name) => {
                let c = CString::new(name.as_bytes()).map_err(|_| invalid("NUL in path"))?;
                let rc = unsafe { libc::mkdirat(root.as_raw_fd(), c.as_ptr(), 0o700) };
                if rc < 0 && io::Error::last_os_error().kind() != io::ErrorKind::AlreadyExists {
                    return Err(io::Error::last_os_error());
                }
                root = openat(
                    &root,
                    name,
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )?;
            }
            _ => return Err(invalid("parent traversal is not allowed")),
        }
    }
    Ok(root)
}

pub fn open_safe_source_file(root_fd: &File, relative_path: &Path) -> io::Result<File> {
    let mut dir = root_fd.try_clone()?;
    let mut components = relative_path.components().peekable();
    while let Some(part) = components.next() {
        let Component::Normal(name) = part else {
            return Err(invalid("unsafe relative path"));
        };
        if components.peek().is_none() {
            let file = openat(
                &dir,
                name,
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )?;
            let metadata = file.metadata()?;
            if !metadata.is_file() || metadata.nlink() != 1 {
                return Err(invalid("source must be a single-link regular file"));
            }
            return Ok(file);
        }
        dir = openat(
            &dir,
            name,
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )?;
    }
    Err(invalid("empty relative path"))
}

pub fn read_bounded(root: &File, path: &Path, cap: usize) -> io::Result<(Vec<u8>, u32)> {
    let file = open_safe_source_file(root, path)?;
    let mode = file.metadata()?.mode();
    let mut bytes = Vec::new();
    file.take((cap + 1) as u64).read_to_end(&mut bytes)?;
    if bytes.len() > cap {
        return Err(invalid("file exceeds per-record cap"));
    }
    Ok((bytes, mode))
}

pub fn regular_file_at(parent: &File, name: &OsStr) -> io::Result<Option<File>> {
    match openat(
        parent,
        name,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
    ) {
        Ok(file) => {
            let meta = file.metadata()?;
            if !meta.is_file() || meta.nlink() != 1 {
                return Err(invalid("bundle file must be a single-link regular file"));
            }
            Ok(Some(file))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}
