# Castor — Worklog

A living collaboration surface between agents. Organized by state, not chronology.
Newest entries first within each section. Prune aggressively — git is the permanent record.

**Conventions:** Tag entries `[CC]` (Claude Code) or `[RA]` (Research Agent) for provenance.

---

## Current Focus

- [CC] **Phase 1 COMPLETE** — All milestones (M1-M4) delivered. 170 tests, 0 lint errors. Docs regenerated for review.

---

## Milestones Delivered

### M4: Sub-Agent Spawning & Integration (Complete)
- [CC] **Sync spawn** — `spawn_agent` syscall: delegate caps, run child, reclaim. PID format `{parent}::{name}-{N}`.
- [CC] **Async spawn/join** — `spawn_agent_async` + `join_agent` for fan-out/fan-in parallelism. Budget delegated at spawn, reclaimed at join.
- [CC] **Child HITL propagation** — child suspends -> parent records child_checkpoint -> parent suspends -> HITLHandler resolves child then resumes.
- [CC] **AgentRegistry** — `register/get/list` + `@castor_agent` decorator.
- [CC] **CLI for HITL** — `castor list|show|reject|modify` via `[project.scripts]`. Approve excluded (requires runtime). Child HITL guarded.
- [CC] **PID collision fix** — shared spawn counter counts both sync and async to prevent collision.
- [CC] **Budget leak guard** — async spawn wraps post-delegation in try/except that reclaims on failure.

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

- **Phase 2 planning:** When to start Rust/PyO3 core? Which subsystem first (Dam validation is hot path)?
- **Async spawn observability:** Child checkpoints not persisted at spawn time (only at join). Orphaned tasks on parent preemption produce warnings but are GC'd.
- **Streaming IPC:** Children cannot stream partial results back to parent. Requires design.
- **Bidirectional IPC:** Parent cannot send follow-up instructions to running child.
- **Recursive spawning:** Children spawning their own sub-agents (capability cascading).
- **Crash recovery:** If kernel process dies mid-execution, checkpoint may be stale.
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

---

## Architecture Snapshot

```
Agent Function
  └── SyscallProxy  (only interface to kernel)
        ├── Dam        — tool registry, Pydantic validation, execution
        ├── Stream     — checkpoint/replay, HITL, persistence
        ├── Lodge      — context paging, eviction, search_memory
        ├── Capability — budget tracking, delegation, refund
        └── LLM        — replay-safe inference wrapper
```

**Milestones:** M1 (Dam+Cap) DONE | M2 (Stream) DONE | M3 (Lodge) DONE | M4 (Integration) DONE

**Stats:** 170 tests | 0 lint errors | 20 public API exports | Python 3.11+ / Pydantic V2 / SQLite

---

## Build

```bash
uv sync && uv run pytest tests/ -v && uv run ruff check src/ tests/
```
