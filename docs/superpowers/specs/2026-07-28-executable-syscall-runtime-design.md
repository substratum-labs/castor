# Executable Syscall Runtime Design

## Goal

Expose the verified syscall state machine as a public Rust runtime API that
PyO3 bindings can call through the standard Cargo build, while retaining Verus
proofs of its correspondence to the existing specification.

## Architecture

`kernel/src/spec.rs` remains the ghost/specification layer. A new public
runtime module will define ordinary Rust enums and a state container using
`Vec` and `BTreeSet`, then provide verified `exec fn` transitions. Each
transition declares an `ensures` clause relating its runtime result to the
existing specification transition.

The library root exports the runtime module normally; the verification root
imports both specification and runtime source files so the same executable
definitions are checked by Verus and compiled by Cargo. The previous
verification-only syscall module is replaced rather than duplicated.

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

Verus contracts prove each successful executable transition refines the
matching spec transition and preserves the state invariant. Runtime unit tests
exercise normal Cargo-visible behavior: proposal/commit, revoked-capability
TOCTOU rejection, state-mode rejection, and public API construction. The
acceptance checks are the local Verus invocation and `cargo check` for
`kernel/`.
