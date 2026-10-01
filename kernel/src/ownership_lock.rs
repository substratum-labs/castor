//! Private process-owned writer guard shared by durable Core and adapter stores.
use std::fs::{File, OpenOptions};
use std::io;
use std::path::Path;

pub(crate) struct OwnershipLock {
    file: File,
    owner_pid: u32,
}

pub(crate) fn acquire_ownership_lock(root: &Path, name: &str) -> io::Result<OwnershipLock> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(root.join(name))?;
    file.try_lock()?;
    let guard = OwnershipLock {
        file,
        owner_pid: std::process::id(),
    };
    File::open(root)?.sync_all()?;
    Ok(guard)
}

impl Drop for OwnershipLock {
    fn drop(&mut self) {
        // Closing alone leaves flock held while an unrelated fork/exec child
        // retains a copy of this open file description. End ownership here.
        // A forked copy must never unlock the acquiring process's live owner.
        if self.owner_pid == std::process::id() {
            let _ = self.file.unlock();
        }
        // File close remains the fallback if unlocking fails. A subsequent
        // acquisition still fails closed rather than bypassing the lock.
    }
}
