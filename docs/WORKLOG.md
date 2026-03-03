# Castor — Worklog

A living collaboration surface between agents. Organized by state, not chronology.
Newest entries first within each section. Prune aggressively — git is the permanent record.

**Conventions:** Tag entries `[CC]` (Claude Code) or `[RA]` (Research Agent) for provenance.

---

## Current Focus

- [CC] **Phase 2.1: Rust Coprocessor** — Maturin scaffold + CapabilityManager in Rust. See `docs/PHASE2_PLAN.md`.

---

## Phase 2 Decisions

- [CC] **Rust as coprocessor, not microkernel** — asyncio.Task.cancel() and contextvars can't cross PyO3 FFI. Rust accelerates pure computation; Python keeps async runtime.
- [CC] **Single `kernel_call()` dispatch** — One FFI entry point mirrors `proxy.syscall()` pattern. Future-proof for new kernel ops without API changes.
- [CC] **CapabilityManager first** — Pure arithmetic, no async, smallest blast radius. Proves PyO3 toolchain.
- [CC] **Dam stays in Python** — `inspect.signature()`, `pydantic.create_model()`, NL error formatting all require Python runtime. Architect's "Dam → Rust" proposal rejected.
- [CC] **Skip serde for models** — Pydantic V2 already uses Rust backend (pydantic-core). Replacing it with raw serde is marginal gain, significant API loss.
- [CC] **Golden tests over shadow mode** — Record Python outputs, assert Rust matches exactly. No parallel implementation maintenance.
- [CC] **rusqlite deferred to Phase 2.2** — Crash-proof journal is a safety invariant (not perf). Too much scope for 2.1.
- [CC] **Project restructure** — `src/castor/` → `python/castor/` for clear language boundary. Maturin convention.
- [CC] **Python prototype preserved** — Tag `v0.1.0-python` + branch `python-prototype` for reference.

---

## Milestones Delivered

### M5: Streaming LLM & Preemption (Complete)
- [CC] **StreamingLLMSyscall** — Async generator wrapper with ContextVar-based partial_work capture.
- [CC] **Token-level preemption** — CancelledError at each chunk iteration; partial text saved to checkpoint.
- [CC] **Proportional budget** — Refund-then-re-deduct pattern via `proxy.charge_partial()`.
- [CC] **Resume context** — `preemption_reason`, `preemption_payload`, `partial_work` fields cleared on success.
- [CC] **on_chunk callbacks** — Sync and async callbacks fired per chunk.

### M4: Sub-Agent Spawning & Integration (Complete)
- [CC] **Sync spawn** — `spawn_agent` syscall: delegate caps, run child, reclaim. PID format `{parent}::{name}-{N}`.
- [CC] **Async spawn/join** — `spawn_agent_async` + `join_agent` for fan-out/fan-in parallelism. Budget delegated at spawn, reclaimed at join.
- [CC] **Child HITL propagation** — child suspends -> parent records child_checkpoint -> parent suspends -> HITLHandler resolves child then resumes.
- [CC] **AgentRegistry** — `register/get/list` + `@castor_agent` decorator.
- [CC] **CLI for HITL** — `castor list|show|reject|modify` via `[project.scripts]`. Approve excluded (requires runtime). Child HITL guarded.
- [CC] **PID collision fix** — shared spawn counter counts both sync and async to prevent collision.
- [CC] **Budget leak guard** — async spawn wraps post-delegation in try/except that reclaims on failure.
- [CC] **Child crash HITL tests** — sync and async variants verify parent unblocked, child FAILED, budget reclaimed.
- [CC] **O(1) spawn counter** — `_spawn_count` cached at proxy init, incremented per spawn.

### M3: Castor Lodge — Context Window Management (Complete)
- [CC] **CastorLodge** — Token monitoring, FIFO eviction, watermark threshold.
- [CC] **Pinned messages** — `CastorMessage.pinned=True` messages never evicted.
- [CC] **SemanticMemoryDriver ABC** — HAL for cold storage backends. `InMemoryDriver` for testing.
- [CC] **TokenCounter protocol** — `CharCountEstimator` default (no tiktoken dependency).
- [CC] **Replay safety** — Eviction routes through `proxy.syscall("sys_kernel_page_out")`. Kernel tool records auto-skipped during replay. Eviction hook fires only during live execution.
- [CC] **`search_memory` tool** — User-facing tool for LLM-initiated memory recall.

### M2: Castor Stream — Checkpoint/Replay (Complete)
- [CC] **SyscallProxy** — 9-step pipeline: lodge hook -> kernel skip -> replay -> spawn intercept -> validate -> HITL gate -> deduct -> execute -> log.
- [CC] **AgentRunner** — Three exit modes: completion, cooperative suspend, preemption.
- [CC] **HITLHandler** — approve (execute + log), reject (feedback), modify (feedback + re-plan). Child HITL variants.
- [CC] **CheckpointStore** — SQLAlchemy + SQLite persistence (upsert, load, delete, list).
- [CC] **ReplayDivergenceError** — Detects agent function divergence from recorded log.
- [CC] **LLMSyscall** — Wraps async LLM clients as replay-safe Castor tools.

### M1: Castor Dam + Capability Manager (Complete)
- [CC] **ToolRegistry** — Name-to-metadata dict with register/get/has/list.
- [CC] **@castor_tool decorator** — Introspects function signature, generates Pydantic JSON Schema, registers tool.
- [CC] **CastorDam** — Validates arguments via lazy-built Pydantic models. Formats validation errors as natural language feedback.
- [CC] **CapabilityManager** — Token-bucket quota system with atomic delegate/reclaim.

---

## Open Questions

_Questions needing exploration or a design decision._

- **Stage 2 gate:** Is ToolRegistry migration worth the effort? Need profiling data.
- **Async spawn observability:** Child checkpoints not persisted at spawn time (only at join). Orphaned tasks on parent preemption produce warnings but are GC'd.
- **Streaming IPC:** Children cannot stream partial results back to parent. Requires design.
- **Bidirectional IPC:** Parent cannot send follow-up instructions to running child.
- **Recursive spawning:** Children spawning their own sub-agents (capability cascading).
- **Multi-tenancy:** Currently single agent tree per process.

---

## Decisions Made

_Resolved questions with brief rationale._

- [CC] **Checkpoint/Replay model** — Python coroutines can't be pickled. Syscall log is the replay journal. Resume = replay from top with cached responses.
- [CC] **LLM calls as syscalls** — LLM inference routed through proxy for replay safety. `LLMSyscall` wrapper prevents `ReplayDivergenceError`.
- [CC] **Transactional deduction** — `deduct()` before `execute()` with `refund()` on failure. Avoids `asyncio.shield()` complexity.
- [CC] **HITL modification** — Never mutate `pending_hitl`. Log `HITL_MODIFIED` with feedback. LLM re-plans on replay. Preserves determinism.
- [CC] **SuspendInterrupt naming** — Interrupt, not error. `# noqa: N818`.
- [CC] **`__annotations__` defensiveness** — `getattr(func, '__annotations__', None) or {}` handles mocks and builtins.
- [CC] **Separate async spawn/join** — `spawn_agent_async` returns handle, `join_agent` blocks. Fan-out/fan-in pattern. HITL propagates at join time.
- [CC] **Shared PID counter** — Counts both sync and async spawns to prevent PID collision.
- [CC] **Lodge eviction via proxy** — `sys_kernel_page_out` is a kernel tool routed through `proxy.syscall()`. Lodge never checks `is_replaying`.
- [CC] **id() set for eviction** — Object identity matching to remove evicted messages. No copies before eviction.
- [CC] **Phase 2 boundary** — CapabilityManager → Rust. Dam/Stream/Lodge/LLM stay Python. See `docs/PHASE2_PLAN.md`.

---

## Architecture Snapshot

```
Agent Function (Python)
  └── SyscallProxy (Python — async runtime, replay, HITL)
        ├── CastorKernel (Rust — via PyO3 kernel_call())
        │     └── CapabilityManager — budget math, delegation
        ├── Dam        — tool registry, Pydantic validation, execution
        ├── Stream     — checkpoint/replay, HITL, persistence
        ├── Lodge      — context paging, eviction, search_memory
        └── LLM        — replay-safe inference wrapper
```

**Phase 1:** M1-M5 DONE | **Phase 2.1:** Rust CapabilityManager (in progress)

**Stats:** 219 tests | 0 lint errors | Python 3.11+ / Pydantic V2 / SQLite / Rust (PyO3)

---

## Build

```bash
maturin develop && uv run pytest tests/ -v && uv run ruff check python/ tests/
```
