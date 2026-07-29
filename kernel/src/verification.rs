//! Dedicated Verus verification root for the kernel model and syscall contracts.

#[path = "spec.rs"]
pub mod spec;

#[path = "syscalls.rs"]
pub mod syscalls;

#[path = "runtime.rs"]
pub mod runtime;
