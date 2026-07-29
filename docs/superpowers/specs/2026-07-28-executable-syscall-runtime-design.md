# Executable Syscall Runtime Design

## Goal

Expose the verified syscall state machine as a public Rust runtime API that
PyO3 bindings can call through the standard Cargo build, while retaining Verus
proofs of its correspondence to the existing specification.

## Architecture

`kernel/src/spec.rs` and `kernel/src/syscalls.rs` remain the ghost model and
its transition contracts. A new public runtime module defines ordinary Rust
enums and a state container using `Vec` and `BTreeSet`, then provides verified
`exec fn` transitions. Its contracts prove the runtime cursor invariant and
the mode, journal-length, and cursor postconditions for every successful
transition.

The library root exports the runtime module normally; the verification root
imports the specification, pure transition contracts, and runtime source so
the same executable definitions are checked by Verus and compiled by Cargo.
The proof-only syscall module remains as the model-level companion to the
runtime state machine.

## Runtime API

The module exposes `Effect`, `Capability`, `JournalRecordType`,
`JournalEntry`, `AgentState`, and `KernelState`, with a `KernelState::new()`
constructor. Public transition functions are `grant_capability`,
`revoke_capability`, `syscall_propose`, `syscall_commit`, `syscall_reject`,
`fault_preempt`, and `resume_execution`.

Transitions that can be disallowed return `Result<KernelState, SyscallError>`.
Proposal requires a running agent and the required current capability.
Commit requires a pending agent and re-evaluates the required current
capability immediately before recording a committed journal entry. A
capability revoked after proposal therefore causes commit to return an
authorization error and leaves the input state unchanged. Reject requires a
pending agent but does not require a capability.

## Verification and Tests

Verus verifies the executable transitions' invariant and state-change
contracts, while the existing pure transition contracts continue to specify
the abstract model. Runtime integration tests exercise normal Cargo-visible
behavior: proposal/commit and revoked-capability TOCTOU rejection. The
acceptance checks are the local Verus invocation and `cargo check` for
`kernel/`.
