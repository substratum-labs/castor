# Castor — Worklog

A living collaboration surface between agents. Organized by state, not chronology.
Newest entries first within each section. Prune aggressively — git is the permanent record.

**Conventions:** Tag entries `[CC]` (Claude Code) or `[RA]` (Research Agent) for provenance.

---

## Current Focus

- [CC] **LLM replay safety** — `LLMSyscall` wrapper shipped. All LLM inference must flow through `proxy.syscall()`. 3 replay-determinism tests passing.
- [CC] **Budget leak fix** — Fast-path `deduct` → `execute` is now transactional via `try/except BaseException` + `refund()`. 3 tests (tool exception, CancelledError, success sanity).

---

## Open Questions

_Questions needing exploration or a design decision. Research Agent can pick these up._

- **Lodge design (M3):** What token counting library? Should eviction use summarization (LLM call) or truncation? How does page-in search work — embedding similarity or keyword?
- **Sub-agent spawning (M4):** `spawn_agent` is a syscall, but should `spawn_agent_async` + `join_agent` be separate syscalls or a single `fan_out` primitive? How does HITL propagate from child to parent?
- **Agent return value:** `AgentRunner` discards `await agent_fn(proxy)` result. Should it be stored in `AgentCheckpoint`? New field, or overload `partial_work`?

---

## Research Notes

_Findings from deep-dives. Reference material for implementation._

_(empty — Research Agent can add findings here)_

---

## Decisions Made

_Resolved questions with brief rationale. Prune once absorbed into code/docs._

- [CC] **LLM calls as syscalls** — Non-deterministic LLM calls must go through proxy to get logged in `syscall_log`. `LLMSyscall` wrapper registers a `@castor_tool` backed by user's async callable. Prevents `ReplayDivergenceError` on resume.
- [CC] **Transactional deduction** — `deduct()` before `execute()` with `refund()` on failure. Avoids `asyncio.shield()` complexity. `refund()` clamps at zero for safety.
- [CC] **`__annotations__` defensiveness** — Both `_generate_schema` and `_build_input_model` use `getattr(func, '__annotations__', None) or {}` to handle mocks and other callables without annotations.
- [CC] **HITL modification** — Never mutate `pending_hitl` args. Log `HITL_MODIFIED` and let LLM re-plan. Preserves replay determinism.
- [CC] **SuspendInterrupt naming** — It's an interrupt, not an error. `# noqa: N818`.

---

## Next Actions

_Concrete tasks ready for implementation. Ordered by priority._

1. **Lodge context pager (M3)** — Token counting, pinning, paging threshold, eviction, page-in. Design docs exist in `docs/`. Needs research on token counting approach first (see Open Questions).
2. **Sub-agent spawning** — Data models ready (`child_checkpoint` in `SyscallRecord`). Needs `spawn_agent` syscall handler in `SyscallProxy`, capability delegation to child, child suspension propagation.
3. **Agent return value** — Store result of `await agent_fn(proxy)` somewhere in `AgentCheckpoint`.
4. **CLI/API for HITL** — Currently requires programmatic `HITLHandler` calls. Needs a user-facing interface.

---

## Architecture Snapshot

```
Agent Function
  └─► SyscallProxy  (only interface to kernel)
        ├─ Dam        — tool registry, Pydantic validation, execution
        ├─ Stream     — checkpoint/replay, HITL, persistence
        ├─ Lodge      — context paging (stub)
        ├─ Capability — budget tracking, delegation, refund
        └─ LLM        — replay-safe inference wrapper
```

**Milestones:** M1 (Dam+Cap) ✅ | M2 (Stream) ✅ | M3 (Lodge) ⏳ | M4 (Integration) ⚙️

**Stats:** 100 tests | 0 lint errors | 15 public API exports | Python 3.11+ / Pydantic V2 / SQLite

---

## Data Models (quick reference)

```
AgentCheckpoint { pid, status, agent_function_name, capabilities, syscall_log, pending_hitl, context_history, preemption_* }
SyscallRecord   { request, response, was_hitl, child_checkpoint }
Capability      { resource_type, max_budget, current_usage }
SyscallResponse { status, result_payload, feedback_message, human_feedback }
```

---

## Build

```bash
uv sync && uv run pytest tests/ -v && uv run ruff check src/ tests/
```
