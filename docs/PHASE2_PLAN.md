# Castor Phase 2 — Rust Coprocessor Migration

**Status:** APPROVED
**Baseline:** `v0.1.0-python` tag / `python-prototype` branch (219 tests, M1-M5)
**Target:** v0.2.0 — Rust-backed CapabilityManager behind a unified kernel FFI

---

## 1. Strategic Context

### 1.1 Why Rust?

The Python prototype validates the architecture. Rust migration targets three goals:

1. **Type safety** — Float-based budget arithmetic benefits from Rust's type system (no accidental precision drift, overflow-safe delegation chains)
2. **Crash-proof journal** (future) — A Rust-owned SQLite journal survives Python process crashes (segfault in C extensions, numpy OOM). This is a **safety invariant**, not a performance optimization.
3. **Extensibility** — Concurrent budget deduction with atomics, lock-free capability checks for multi-tenant deployments

### 1.2 Why NOT Full Rust Microkernel?

The original architect vision proposed replacing asyncio with Tokio and moving all subsystems to Rust. This was rejected for three reasons:

| Concern | Detail |
|---|---|
| `asyncio.Task.cancel()` | Token-level preemption (StreamingLLMSyscall) relies on CancelledError propagation through Python async generators. Crossing PyO3 FFI with Tokio futures is unsolved. |
| `contextvars` | Partial-work capture uses per-task ContextVars. These have no Rust equivalent across FFI. |
| Dam validation | `inspect.signature(func)` introspects Python callables. `pydantic.create_model()` builds dynamic validation models. Natural language error formatting drives LLM self-correction. All require Python. |
| Pydantic V2 already uses Rust | `pydantic-core` is a Rust crate via maturin. Replacing it with raw serde is swapping one Rust backend for another — marginal gain, significant API loss. |

**Conclusion:** Rust serves as a **coprocessor/accelerator** for pure computation, not a replacement for the Python async runtime.

---

## 2. Boundary Split

### 2.1 What Moves to Rust (Stage 1)

| Component | Lines | Rationale |
|---|---|---|
| `Capability` model | 12 | Pure data struct, no Pydantic features needed |
| `CapabilityManager` | 132 | Pure arithmetic (deduct/refund/delegate/reclaim), no I/O, no async |
| `CapabilityExhaustedError` | 10 | Maps to Rust error type |
| `InsufficientBudgetError` | 10 | Maps to Rust error type |

### 2.2 What Stays in Python

| Component | Reason |
|---|---|
| `SyscallProxy` | asyncio Task lifecycle, CancelledError handling, contextvars |
| `AgentRunner` | asyncio.create_task, preemption via Task.cancel() |
| `CastorDam` / `CastorDam.validate()` | inspect.signature, pydantic.create_model, NL error formatting |
| `@castor_tool` decorator | Python function introspection |
| `HITLHandler` | Orchestrates AgentRunner (async), no hot-path computation |
| `CastorLodge` | Token counting, eviction via proxy.syscall (needs Python proxy) |
| `CheckpointStore` | SQLAlchemy ORM, WAL, gc_orphans, recover |
| `LLMSyscall` / `StreamingLLMSyscall` | ContextVar-based partial_work, CancelledError handling |

### 2.3 Dual-Layer Validation (Architect-Approved)

- **User space (Python):** Pydantic validates `@castor_tool` arguments. LLM-facing error messages must be natural language for self-correction.
- **Kernel space (Rust):** Rust types enforce internal invariants (capability budgets, delegation chains). Errors are programmatic, not LLM-facing.

---

## 3. FFI Architecture

### 3.1 Single Dispatch Entry Point

Inspired by the architect's original vision, the Rust boundary exposes ONE entry point:

```rust
#[pyclass]
struct CastorKernel {
    capabilities: HashMap<String, Capability>,
}

#[pymethods]
impl CastorKernel {
    /// Single dispatch point — mirrors SyscallProxy.syscall() pattern
    fn kernel_call(&self, op: &str, args: Py<PyDict>) -> PyResult<PyObject> {
        match op {
            "cap_create" => { ... }
            "cap_deduct" => { ... }
            "cap_refund" => { ... }
            "cap_delegate" => { ... }
            "cap_reclaim" => { ... }
            "cap_check" => { ... }
            _ => Err(PyValueError::new_err(format!("unknown op: {op}")))
        }
    }
}
```

**Why single dispatch:**
- Python-side interface doesn't change when we add new Rust operations
- Mirrors the existing `proxy.syscall(tool_name, args)` pattern — the OS metaphor becomes physical
- Single point for logging, metrics, and future kernel-level access control
- Adding ToolRegistry later is a kernel-internal change, not an API change

### 3.2 Python Integration Point

`SyscallProxy` calls `kernel.kernel_call()` for budget operations instead of `self._cap_mgr.deduct()`:

```python
# Before (pure Python):
self._cap_mgr.deduct(self.checkpoint.capabilities, resource, cost)

# After (Rust kernel):
self._kernel.kernel_call("cap_deduct", {
    "pid": self.checkpoint.pid,
    "resource": resource,
    "cost": cost,
})
```

The proxy still owns the checkpoint Python object. The kernel operates on capability state passed via FFI — it does NOT own the checkpoint.

---

## 4. Stage Roadmap

### Stage 1: Kernel Scaffold + CapabilityManager (4 weeks)

**Deliverables:**
1. Maturin project setup (`Cargo.toml`, `pyproject.toml` integration, CI)
2. Project restructure: `src/castor/` → `python/castor/`
3. `CastorKernel` struct with `kernel_call()` dispatcher
4. `Capability` struct + `CapabilityManager` logic in Rust
5. Python `CapabilityManager` becomes a thin wrapper calling `kernel.kernel_call()`
6. All 219 existing tests pass unchanged
7. Golden test snapshot: record Python outputs before Rust cutover

**Success criteria:** `uv run pytest` passes with Rust kernel backing all capability operations.

**Build:**
```bash
maturin develop          # Build Rust + install into venv
uv run pytest tests/ -v  # All 219 tests pass
```

### Stage 2: ToolMetadata + Registry (2 weeks, optional)

**Gate:** Only proceed if profiling shows dict lookup is a measurable bottleneck (unlikely).

**Deliverables:**
1. `ToolMetadata` struct in Rust (replaces Pydantic model)
2. `ToolRegistry` HashMap in Rust kernel
3. Dam validator reads metadata from kernel via `kernel_call("registry_get", ...)`

**Note:** Dam validation (`_build_input_model`, `format_validation_error`) stays in Python.

### Stage 3: Crash-Proof Journal (future, Phase 2.2)

**Motivation:** Safety, not performance. A Rust-owned SQLite journal (rusqlite) survives Python process crashes.

**Deliverables:**
1. rusqlite for WAL and checkpoint persistence inside Rust kernel
2. Python `CheckpointStore` becomes a thin read-only query layer
3. Kernel owns the journal lifecycle (write_wal, complete_wal, abandon_wal)

**Prerequisite:** Stage 1 stable in production. Profiling data confirming serialization path.

### Stage 4: Cut-Over

**Deliverables:**
1. Golden test suite: assert Rust outputs match recorded Python outputs exactly
2. Property-based tests (Hypothesis) for float edge cases in budget math
3. Delete Python `CapabilityManager` implementation
4. Update all documentation

**Why golden tests, not shadow mode:** Running dual implementations in parallel is expensive to maintain and creates "temporary" code that becomes permanent. Golden tests are cheaper and deterministic.

---

## 5. Risk Register

| Risk | Severity | Mitigation |
|---|---|---|
| PyO3 dict conversion overhead on every syscall | Medium | Benchmark early in Stage 1. If overhead > 1ms, batch operations or use typed structs instead of dicts. |
| Float precision differs between Python and Rust | Medium | Property-based tests (Hypothesis) comparing Python vs Rust for edge cases (subnormals, large values, negative zero). |
| Maturin build adds CI complexity | Low | Pin maturin version, cache Rust toolchain in CI. Test on Linux + macOS. |
| Pydantic error messages differ from Rust errors | Low | Dual-layer validation — Pydantic for user-facing, Rust for kernel-internal. They never mix. |
| Migration breaks replay determinism | Medium | Golden test suite records Python behavior, Rust must match exactly. Run full replay test battery before deleting Python impl. |

---

## 6. What This Plan Explicitly Defers

These items are out of scope for Phase 2.1:

- **Tokio integration** — asyncio stays as the runtime. Revisit only if we hit provable scheduler bottlenecks.
- **serde replacing Pydantic** — Pydantic V2 already uses Rust (pydantic-core). No evidence of serialization bottleneck.
- **Full Dam migration** — Python function introspection and dynamic model creation require Python.
- **Lodge migration** — Eviction routes through Python proxy; token counting is trivial.
- **Shadow mode dual execution** — Replaced by golden test suite (cheaper, no maintenance burden).

---

## 7. Decision Log

| Decision | Rationale | Decided By |
|---|---|---|
| Rust as coprocessor, not microkernel | asyncio.Task.cancel() and contextvars can't cross FFI | CC evaluation, Architect review |
| Single `kernel_call()` dispatch | Mirrors syscall pattern, future-proof for new ops | Architect original plan (adapted) |
| CapabilityManager first | Pure computation, no async, smallest blast radius | CC evaluation |
| Dam stays in Python | inspect.signature, create_model, NL errors need Python | CC pushback on architect |
| Skip serde for models | Pydantic V2 already Rust-backed (pydantic-core) | CC analysis |
| Golden tests over shadow mode | Cheaper, no parallel maintenance burden | CC counter-proposal |
| rusqlite for crash safety (future) | Safety invariant, not perf — journal must survive user-space crash | Architect original plan |
| `src/` → `python/` restructure | Clear language boundary, maturin convention | CC + architect agreement |
