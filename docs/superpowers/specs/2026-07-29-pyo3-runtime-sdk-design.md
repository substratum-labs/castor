# PyO3 Runtime SDK Design

## Goal

Expose the verified `kernel::runtime` state machine to Python through an
in-process PyO3 class without adding durable storage or external I/O to the
Rust core.

## Scope and boundary

`KernelRuntime` owns an in-memory `runtime::KernelState`. It exposes the
verified mechanism already implemented in Rust: capability grant/revoke,
proposal, commit, reject, preemption, resumption, and read-only state
snapshots. Each mutating method delegates directly to its corresponding
verified runtime transition.

The binding accepts the closed Phase-2 vocabulary (`effect_a`/`effect_b` and
`cap_a`/`cap_b`) and returns ordinary Python state values. Unauthorized
transitions become `PermissionError`; invalid state transitions become
`RuntimeError`; unrecognized vocabulary becomes `ValueError`.

The Rust core will not open files, create databases, issue network requests,
or select a persistence backend. Any session persistence remains an SDK or
`castor-server` responsibility, as required by
`KERNEL_SERVER_BOUNDARY.md`.

## Components

- `kernel/src/lib.rs`: conversion helpers, `#[pyclass] KernelRuntime`, and
  module registration.
- `kernel/tests/python_bindings.rs`: PyO3 integration tests that call the
  exported Python class through the initialized interpreter.
- `kernel/pyproject.toml`: Maturin packaging metadata for producing the
  extension module independently of the existing pure-Python SDK package.

## Tests and acceptance

Tests exercise a real `KernelRuntime` object from Python, not a mocked
facade. They prove an authorized propose/commit sequence records the expected
journal and returns to `running`; they also prove a revoked capability maps
the verified TOCTOU rejection to `PermissionError`. Cargo tests compile the
extension and run those interpreter-backed tests; a Maturin wheel build proves
the module can be packaged for Python.
