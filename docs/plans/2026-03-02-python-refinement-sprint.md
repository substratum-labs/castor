# Python Refinement Sprint — v0.1.0 Pre-Release

**Date:** 2026-03-02
**Goal:** Harden architecture, add observability, polish for open-source, freeze API, ship to TestPyPI.
**Duration:** 3 weeks
**Precedes:** Phase 2 Rust/PyO3 core rewrite

---

## Week 1 — Architecture Hardening (Model-Changing Fixes)

These changes alter data models and persistence. They must happen before the Rust port
to avoid porting schemas twice.

### 1.1 Write-Ahead Log for Crash Recovery

**Problem:** If the kernel crashes between tool execution and `_append_record()`,
the budget is deducted but the result is lost. On restart, the syscall re-executes
and re-deducts — a permanent budget leak and potential duplicate side effect.

**Design:**
- Add a `wal_log` table to SQLite alongside the existing `checkpoints` table.
- Before executing a tool, write a WAL entry:
  `{pid, syscall_index, tool_name, arguments, budget_snapshot, status: "PENDING"}`.
- After execution succeeds, update WAL entry to `"COMPLETED"` with the result,
  then append to `syscall_log` as normal.
- On kernel startup, `CheckpointStore.recover()` scans for `PENDING` WAL entries:
  - If found: refund the budget deduction (budget_snapshot tells us what to restore),
    mark WAL entry `"ABANDONED"`.
  - The agent resumes from the last *committed* syscall_log entry — the incomplete
    syscall simply re-executes.
- WAL entries for `"COMPLETED"` operations get garbage collected after successful
  checkpoint persistence.

**Files changed:** `stream/persistence.py`, `stream/proxy.py`

### 1.2 Async Spawn Observability

**Problem:** Child checkpoints from `spawn_agent_async` only exist in parent memory
until `join_agent`. If parent is preempted between spawn and join, child tasks are
orphaned with no persisted state.

**Design:**
- Persist child checkpoint to `CheckpointStore` immediately at spawn time
  (inside `_handle_async_spawn`).
- Add `parent_pid` index to enable querying "all children of process X".
- Add `Kernel.gc_orphans()`: finds children whose parent is `COMPLETED`/`FAILED`
  but child is still `RUNNING` → mark as `ORPHANED`.
- `AgentRunner` calls `store.save(child_checkpoint)` after creating the child task.

**Files changed:** `stream/proxy.py`, `stream/runner.py`, `stream/persistence.py`

### 1.3 Unify `_resume_child()` with `AgentRunner.run()`

**Problem:** `HITLHandler._resume_child()` duplicates agent-running logic
(create proxy, run function, handle SuspendInterrupt).

**Design:**
- Extract common logic into `AgentRunner.run()` accepting an optional existing
  checkpoint.
- `_resume_child()` becomes a thin wrapper: resolve child HITL, then call
  `runner.run(agent_name, checkpoint=child_cp)`.
- Runner already handles all three exit modes (complete, suspend, preempt).

**Files changed:** `stream/hitl.py`, `stream/runner.py`

### 1.4 Tool Execution Timeouts

**Problem:** CPU-bound tools with no `await` points block the event loop and
resist `task.cancel()`.

**Design:**
- Add optional `timeout_seconds: float | None` parameter to `@castor_tool`.
- In `SyscallProxy`, for tools with timeout: `asyncio.wait_for()` for async tools,
  `loop.run_in_executor(ProcessPoolExecutor)` for sync tools.
- Default: no timeout (backwards compatible).
- Budget refund on timeout (same as execution failure).

**Files changed:** `dam/decorator.py`, `stream/proxy.py`

---

## Week 2 — Open-Source Polish + Observability

### 2.1 Fix README

- Update test count (90 → actual).
- Update Lodge status: "Planned" → "Complete".
- Add badges: CI status, PyPI version (placeholder), license, Python versions.
- Add 15-line inline quickstart example.

**Files changed:** `README.md`

### 2.2 Community Files

- `CONTRIBUTING.md` — dev setup, test/lint commands, PR expectations, architecture
  overview.
- `CODE_OF_CONDUCT.md` — Contributor Covenant.
- `.github/ISSUE_TEMPLATE/bug_report.md` — repro steps, expected/actual, Python version.
- `.github/ISSUE_TEMPLATE/feature_request.md` — use case, proposed solution.
- `.github/PULL_REQUEST_TEMPLATE.md` — checklist.

**Files added:** 5 new files

### 2.3 Observability — Structured Logging + OpenTelemetry + Metrics

Three layers, all guarded by optional import:

**Layer 1: Structured logging** (zero new deps)
- Python `logging` at key proxy pipeline points: syscall entry/exit, replay hit,
  HITL suspend/resume, budget deduct/refund, spawn/join.
- JSON-structured: `{"event": "syscall_execute", "pid": "...", "tool": "...", "latency_ms": ...}`
- Logger names: `castor.stream`, `castor.dam`, `castor.capability`, `castor.lodge`.

**Layer 2: OpenTelemetry tracing** (optional dep)
- Spans: `castor.syscall`, `castor.replay`, `castor.hitl`.
- Span attributes: tool_name, pid, is_replay, was_hitl.
- Parent-child span linking for spawn/join.
- Guarded: `try: import opentelemetry ... except ImportError: noop`.

**Layer 3: Prometheus-style metrics** (optional dep)
- Counters: `castor_syscalls_total{tool, status}`, `castor_hitl_total{action}`,
  `castor_spawns_total`.
- Histograms: `castor_syscall_duration_seconds{tool}`,
  `castor_replay_duration_seconds`.
- Gauges: `castor_budget_remaining{resource}`, `castor_context_tokens{pid}`.
- Same import guard — noop if not installed.

**Dependency:**
```toml
[project.optional-dependencies]
observability = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
```

**Files changed:** `stream/proxy.py`, `stream/runner.py`, `stream/hitl.py`,
`capability/manager.py`, `lodge/core.py`, `pyproject.toml`
**Files added:** `castor/observability.py`

### 2.4 Quickstart Example

Self-contained runnable file: register a tool, write an agent, run it.
Also embedded in README.

**Files added:** `examples/quickstart.py`

---

## Week 3 — API Freeze + Hardening + Ship

### 3.1 Stable/Experimental Markers

Decorators in `castor/api_status.py`:
- `@stable` — public API, will not break between minor versions.
- `@experimental` — may change in future versions.

No runtime enforcement — informational only, shows in `help()`.

**Stability assignments:**

| API | Status | Rationale |
|-----|--------|-----------|
| `SyscallProxy`, `proxy.syscall()` | stable | Core contract |
| `@castor_tool`, `CastorDam` | stable | Tool registration is foundational |
| `AgentCheckpoint`, `SyscallRecord`, `Capability` | stable | Data models / Rust FFI boundary |
| `HITLHandler` (approve/reject/modify) | stable | Core HITL contract |
| `CapabilityManager` | stable | Budget math is settled |
| `AgentRunner.run()` | stable | Main execution entry point |
| `CheckpointStore` | stable | Persistence contract |
| `CastorLodge` | experimental | Eviction strategy may evolve |
| `SemanticMemoryDriver` | experimental | HAL interface may grow |
| `LLMSyscall` | experimental | Wrapper API may change |
| `AgentRegistry`, `@castor_agent` | experimental | Spawn model may evolve |
| `castor.observability` | experimental | New, needs field validation |

**Files added:** `castor/api_status.py`
**Files changed:** `__init__.py`, public classes/functions across modules

### 3.2 Property-Based Tests (Hypothesis)

Properties to verify:
1. **Replay identity:** Any valid syscall sequence, replayed from checkpoint,
   produces identical return values at every step.
2. **Budget conservation:** `initial_budget == current_usage + remaining` holds
   at every point, including delegate/reclaim/refund.
3. **HITL modify invariant:** Original request is always preserved in the log.
4. **Spawn budget isolation:** Child budget never exceeds delegated amount,
   parent reclaims exactly what child didn't spend.
5. **WAL recovery:** Simulate crash at any point in syscall pipeline, recover,
   assert no budget leak and no duplicate side effects.

Add `hypothesis` as test dependency.

**Files added:** `tests/test_property_based.py`

### 3.3 Benchmark Python Baseline

Microbenchmarks for the numbers Rust needs to beat:

| Metric | Measurement |
|--------|-------------|
| Syscall latency (fast path) | proxy.syscall() → return, non-destructive |
| Syscall latency (replay path) | Cached response from syscall_log |
| Dam validation time | Pydantic schema validation per call |
| Checkpoint serialization | model_dump_json() at 10/100/1000 syscalls |
| Checkpoint persistence | SQLite write round-trip at various sizes |
| Budget operations | Deduct/refund/delegate/reclaim cycle |
| Lodge eviction | FIFO eviction + token counting |

10k iterations, report p50/p95/p99 via `time.perf_counter_ns`.

**Files added:** `benchmarks/bench_baseline.py`

### 3.4 Publish to TestPyPI

- Verify `pyproject.toml` metadata.
- `uv build` → inspect wheel.
- Test install in clean venv.
- Publish to TestPyPI only. Real PyPI deferred to v0.2.0 (Rust core).
- Add `CHANGELOG.md` with v0.1.0 release notes.

**Files added:** `CHANGELOG.md`
**Files changed:** `pyproject.toml`

---

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Crash recovery approach | WAL | Most robust; changes persistence model (do before Rust port) |
| Observability depth | Logging + OTel + metrics | Full stack; optional deps via extras |
| API stability mechanism | @stable/@experimental decorators | Informational, queryable, no runtime cost |
| PyPI publication | TestPyPI only | Gather GitHub feedback first; real PyPI at v0.2.0 |
| mini-castor | Stays in separate repo | Not moved into main repo for v0.1.0 |

## Out of Scope

- Rust/PyO3 rewrite (Phase 2, post this sprint)
- Streaming IPC, bidirectional IPC
- Recursive spawning beyond current depth
- Multi-tenancy
- Real SemanticMemoryDriver implementation
- Constellation distributed orchestration
- Real PyPI publication
