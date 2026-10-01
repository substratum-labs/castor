//! Writer ownership must end with its owner, even during another thread's fork/exec.
use castor_kernel::c01_storage::D1DurableStorage;
use castor_kernel::c04_adapter::{
    D1EffectAdapter, DispatchCommand, EffectProvider, ProviderOutcome,
};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::process::Command;
use std::time::Duration;

fn during_fork_window<T>(during: impl FnOnce() -> T) -> T {
    let (mut parent, child) = UnixStream::pair().unwrap();
    parent
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    child
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let process = std::thread::spawn(move || {
        let mut command = Command::new("true");
        // Force the normally brief fork/exec window to remain open. The child
        // only performs async-signal-safe syscalls until returning to exec.
        unsafe {
            command.pre_exec(move || {
                let mut byte = 1_u8;
                if libc::write(child.as_raw_fd(), (&byte as *const u8).cast(), 1) != 1
                    || libc::read(child.as_raw_fd(), (&mut byte as *mut u8).cast(), 1) != 1
                {
                    return Err(std::io::Error::from_raw_os_error(libc::EIO));
                }
                Ok(())
            });
        }
        command.status()
    });
    let mut ready = [0_u8];
    let handshake = parent.read_exact(&mut ready);
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(during));
    // Always release/join the child before asserting, including the RED case.
    let released = parent.write_all(&[1]);
    let status = process.join().unwrap();
    handshake.unwrap();
    released.unwrap();
    assert!(status.unwrap().success());
    match outcome {
        Ok(value) => value,
        Err(panic) => std::panic::resume_unwind(panic),
    }
}

#[test]
fn core_writer_reopens_while_an_unrelated_child_holds_inherited_descriptors() {
    let root = tempfile::tempdir().unwrap();
    let owner = D1DurableStorage::open(root.path()).unwrap();
    assert!(D1DurableStorage::open(root.path()).is_err());
    let reopened = during_fork_window(|| {
        drop(owner);
        D1DurableStorage::open(root.path())
    })
    .expect("dropping the writer must release ownership before child exec");
    assert!(D1DurableStorage::open(root.path()).is_err());
    drop(reopened);
    D1DurableStorage::open(root.path()).unwrap();
}

struct NoProvider;
impl EffectProvider for NoProvider {
    fn submit(&mut self, _: &DispatchCommand) -> ProviderOutcome {
        panic!("ownership regression must not submit an effect");
    }
}

#[test]
fn adapter_writer_reopens_while_an_unrelated_child_holds_inherited_descriptors() {
    let core = tempfile::tempdir().unwrap();
    let root = tempfile::tempdir().unwrap();
    let owner =
        D1EffectAdapter::initialize(root.path(), core.path(), "adapter", "profile", NoProvider)
            .unwrap();
    let open = || D1EffectAdapter::open(root.path(), core.path(), "adapter", "profile", NoProvider);
    assert!(open().is_err());
    let reopened = during_fork_window(|| {
        drop(owner);
        open()
    })
    .expect("dropping the adapter must release ownership before child exec");
    assert!(open().is_err());
    drop(reopened);
    open().unwrap();
}

#[test]
fn read_only_inspection_does_not_release_the_live_writer() {
    let root = tempfile::tempdir().unwrap();
    let owner = D1DurableStorage::open(root.path()).unwrap();
    D1DurableStorage::inspect(root.path()).unwrap();
    assert!(D1DurableStorage::open(root.path()).is_err());
    drop(owner);
    D1DurableStorage::open(root.path()).unwrap();
}
