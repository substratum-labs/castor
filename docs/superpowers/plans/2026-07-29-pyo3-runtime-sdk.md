# PyO3 Runtime SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the verified Rust runtime callable from Python without giving
the Rust core any persistence or external-I/O responsibility.

**Architecture:** `KernelRuntime` keeps the existing verified
`runtime::KernelState` in memory and delegates every state change to the
existing `runtime` transition. Thin parsing and exception conversion remain in
the PyO3 adapter layer.

**Tech Stack:** Rust, Verus-checked runtime functions, PyO3 0.29, Maturin.

## Global Constraints

- No SQLite, filesystem, network, or other external I/O in `kernel/`.
- Do not duplicate or reimplement verified runtime state transitions.
- Preserve the existing pure-Python SDK packaging at repository root.

---

### Task 1: Prove the Python runtime contract

**Files:**
- Create: `kernel/tests/python_bindings.rs`
- Modify: `kernel/src/lib.rs`

**Interfaces:**
- Consumes: `runtime::{KernelState, grant_capability, revoke_capability, syscall_propose, syscall_commit}`.
- Produces: Python class `castor_kernel.KernelRuntime` with `grant`,
  `revoke`, `propose`, `commit`, `agent_state`, `cursor`, and `journal`.

- [ ] **Step 1: Write failing interpreter-backed tests**

```rust
let runtime = py_kernel.getattr("KernelRuntime")?.call0()?;
runtime.call_method1("grant", ("cap_a",))?;
runtime.call_method1("propose", ("effect_a",))?;
runtime.call_method1("commit", ("effect_a",))?;
assert_eq!(runtime.getattr("agent_state")?.extract::<String>()?, "running");
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `cargo test --manifest-path kernel/Cargo.toml --test python_bindings`
Expected: FAIL because `KernelRuntime` is not exported.

- [ ] **Step 3: Implement the smallest binding adapter**

Add parsers for the closed effect/capability vocabulary, an in-memory
`#[pyclass] KernelRuntime`, direct calls to `runtime` transitions, and module
registration. Map `Unauthorized` to `PyPermissionError` and `InvalidState` to
`PyRuntimeError`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `cargo test --manifest-path kernel/Cargo.toml --test python_bindings`
Expected: PASS.

### Task 2: Package the extension without merging package boundaries

**Files:**
- Create: `kernel/pyproject.toml`

**Interfaces:**
- Consumes: `kernel/Cargo.toml` crate name and `castor_kernel` module name.
- Produces: an independently buildable Maturin extension wheel.

- [ ] **Step 1: Add minimal Maturin metadata**

```toml
[build-system]
requires = ["maturin>=1.7,<2.0"]
build-backend = "maturin"

[project]
name = "castor-kernel-runtime"
version = "0.1.0"
requires-python = ">=3.11"

[tool.maturin]
module-name = "castor_kernel"
```

- [ ] **Step 2: Build a wheel**

Run: `maturin build --manifest-path kernel/Cargo.toml --release -o /tmp/castor-kernel-wheel`
Expected: one `castor_kernel_runtime-0.1.0-*.whl` artifact and exit 0.

### Task 3: Full verification

**Files:**
- Verify: `kernel/src/lib.rs`, `kernel/tests/python_bindings.rs`, `kernel/pyproject.toml`

- [ ] **Step 1: Format and run all kernel tests**

Run: `cargo fmt --manifest-path kernel/Cargo.toml --check && cargo test --manifest-path kernel/Cargo.toml`
Expected: formatting clean and every Rust/PyO3 integration test passes.

- [ ] **Step 2: Verify boundary exclusions**

Run: `rg -n -i 'sqlite|rusqlite|std::fs|tokio|reqwest|http' kernel/src kernel/tests`
Expected: no matches.
