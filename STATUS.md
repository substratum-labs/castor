# Castor — Project Status & Architecture Report

**Date:** 2026-02-28
**Version:** 0.1.0
**Phase:** 1 (Python Prototype)
**Audience:** Staff engineer onboarding / cross-team review

---

## TL;DR

Castor is a **secure microkernel for LLM Agents**. It cages LLMs inside a deterministic execution engine with strongly-typed tool validation, capability-based security budgets, and preemptive human-in-the-loop (HITL) interrupts.

**Current state:** Milestones 1–2 complete. 90 tests passing, 0 lint errors. Three of four kernel subsystems are production-ready. Lodge (context pager) and sub-agent spawning remain.

---

## Milestone Tracker

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M1 — Dam + Capability** | Tool registry, Pydantic validation, budget tracking | ✅ Complete |
| **M2 — Stream** | SyscallProxy, AgentRunner, HITL handler, CheckpointStore | ✅ Complete |
| **M3 — Lodge** | Context window paging, token counting, eviction | ⏳ Not started |
| **M4 — Integration** | Sub-agent spawning, CLI/API for HITL, fan-out/fan-in | ⚙️ Partial (E2E tests done) |
| **Phase 2 — Rust core** | PyO3 rewrite of hot paths | 🔮 Planned |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│                  Agent Function                  │
│          async def agent(proxy) -> Any           │
├─────────────────────────────────────────────────┤
│                  SyscallProxy                    │  ← only interface to kernel
│         await proxy.syscall(tool, args)          │
├──────────┬──────────┬──────────┬────────────────┤
│   Dam    │  Stream  │  Lodge   │   Capability   │
│ validate │ schedule │ context  │   budget       │
│ execute  │ replay   │ paging   │   delegation   │
│          │ persist  │ (stub)   │                │
└──────────┴──────────┴──────────┴────────────────┘
```

**Core invariant:** All side effects flow through `proxy.syscall()`. The agent never touches tools, storage, or the network directly.

### Syscall Lifecycle

```
proxy.syscall(tool, args)
  │
  ├─ Replay? ──yes──► Return cached response (deterministic fast-forward)
  │
  ├─ Validate via Dam
  │     └─ Fail? ──► Return SyscallResponse(VALIDATION_ERROR, feedback)
  │
  ├─ Destructive / requires_hitl? ──yes──► SLOW PATH
  │     Set pending_hitl, raise SuspendInterrupt
  │     Human approves/rejects/modifies → resume via replay
  │
  └─ Safe? ──► FAST PATH
        Deduct capability → Execute tool → Log SyscallRecord → Return
```

---

## Subsystem Detail

### Dam (Tool Registry & Validation) — `src/castor/dam/`

Strongly-typed sandbox. Tools are registered via `@castor_tool` decorator, validated with auto-generated Pydantic schemas, and executed through `CastorDam`.

| Component | File | Purpose |
|-----------|------|---------|
| `ToolRegistry` | `registry.py` | Store/lookup tool metadata |
| `@castor_tool` | `decorator.py` | Register functions with security annotations |
| `CastorDam` | `validator.py` | Validate args, execute tool, format errors for LLM |

Key properties per tool:
- `consumes` — resource type for capability deduction
- `cost_per_use` — numeric cost per invocation
- `destructive` — forces slow path (HITL gate)
- `requires_hitl` — explicit human approval required

**Tests:** 26 (registry 8, decorator 7, validator 11)

---

### Capability Manager — `src/castor/capability/`

Resource budgets that enforce security invariants. Child agents can never exceed parent budgets.

| Method | Behavior |
|--------|----------|
| `create_capabilities(specs)` | Initialize root budgets from `{resource: max}` dict |
| `check(caps, resource, cost)` | Pre-flight: will this deduction succeed? |
| `deduct(caps, resource, cost)` | Atomic deduction; raises `CapabilityExhaustedError` |
| `delegate(parent, requested)` | Carve child budget from parent (all-or-nothing) |
| `reclaim(parent, child)` | Return unused child budget to parent |

**Tests:** 19 (create, check, deduct, delegate, reclaim, full lifecycle)

---

### Stream (Checkpoint/Replay Scheduler) — `src/castor/stream/`

The execution engine. Implements checkpoint/replay rather than coroutine serialization — Python coroutines can't be pickled, so we replay the agent function from the top, serving cached syscall responses until reaching the suspension point.

| Component | File | Purpose |
|-----------|------|---------|
| `SyscallProxy` | `proxy.py` | Replay gateway; fast/slow path routing |
| `AgentRunner` | `runner.py` | Kernel executor; preemption via `task.cancel()` |
| `HITLHandler` | `hitl.py` | Process human approve/reject/modify feedback |
| `CheckpointStore` | `persistence.py` | SQLite persistence via SQLAlchemy ORM |

**Checkpoint/Replay model:**
- Agent state = ordered list of `SyscallRecord` entries (the replay journal)
- Suspend = raise `SuspendInterrupt` to unwind the stack
- Resume = replay function from top, serve cached responses, fast-forward to live execution
- Determinism guarantee: same inputs → same replay sequence

**HITL feedback modes:**
- **Approve** — execute the pending syscall, log result, resume
- **Reject** — log `HITL_REJECTED` with human feedback; agent re-plans on replay
- **Modify** — log `HITL_MODIFIED` with human feedback; original request preserved (never mutated)

**Tests:** 40 (proxy 11, runner 9, hitl 7, persistence 13)

---

### Lodge (Context Pager) — `src/castor/lodge/`

**Status: Stub only.** Empty `__init__.py`.

Planned for M3:
- Token counting for `context_history`
- Pin system instructions as non-evictable
- Paging threshold: detect context window overflow
- Eviction: summarize/compress old messages to local storage
- Page-in: retrieval-augmented context restoration

---

## Data Models

All models are Pydantic V2 `BaseModel` subclasses, fully JSON-serializable.

### `AgentCheckpoint` — the serializable agent state

```python
pid: str
parent_pid: str | None
status: Literal["RUNNING", "SUSPENDED_FOR_HITL", "PREEMPTED", "COMPLETED", "FAILED"]
agent_function_name: str
capabilities: dict[str, Capability]
syscall_log: list[SyscallRecord]          # replay journal
pending_hitl: dict[str, Any] | None       # blocked syscall
context_history: list[dict[str, Any]]
preemption_reason: str | None             # metadata, outside replay
preemption_payload: dict[str, Any] | None
partial_work: str | None
```

### `SyscallRecord` — one logged syscall

```python
request: dict[str, Any]                   # {tool_name, arguments}
response: Any                             # return value or HITL feedback
was_hitl: bool = False
child_checkpoint: AgentCheckpoint | None  # for nested sub-agents
```

### `Capability` — a resource budget

```python
resource_type: str      # e.g., "disk", "network", "api_usd"
max_budget: float
current_usage: float = 0.0
```

### `SyscallResponse` — feedback to agent

```python
status: Literal["SUCCESS", "VALIDATION_ERROR", "HITL_MODIFIED",
                "HITL_REJECTED", "SUSPENDED", "INSUFFICIENT_CAPABILITY"]
result_payload: Any | None
feedback_message: str | None    # validation/capability errors
human_feedback: str | None      # from human during HITL
```

---

## Public API Surface

14 exports from `castor` package:

```python
from castor import (
    AgentCheckpoint, AgentRunner, Capability, CapabilityManager,
    CastorDam, CheckpointStore, HITLHandler, SuspendInterrupt,
    SyscallProxy, SyscallRecord, SyscallRequest, SyscallResponse,
    castor_tool,
)
```

Typical usage:

```python
registry = ToolRegistry()

@castor_tool(consumes="api_usd", cost_per_use=0.01, registry=registry)
async def search(query: str) -> str: ...

dam = CastorDam(registry)
cap_mgr = CapabilityManager()
caps = cap_mgr.create_capabilities({"api_usd": 1.0})
checkpoint = AgentCheckpoint(pid="agent-1", status="RUNNING",
                              agent_function_name="my_agent", capabilities=caps)

runner = AgentRunner(dam, cap_mgr)
result = await runner.run(my_agent, checkpoint)
```

---

## Test Coverage Summary

| Area | File | Tests |
|------|------|------:|
| Dam — Registry | `test_dam_registry.py` | 8 |
| Dam — Decorator | `test_dam_decorator.py` | 7 |
| Dam — Validator | `test_dam_validator.py` | 11 |
| Capability | `test_capability.py` | 19 |
| Stream — Proxy | `test_proxy.py` | 11 |
| Stream — Runner | `test_runner.py` | 9 |
| Stream — HITL | `test_hitl.py` | 7 |
| Stream — Persistence | `test_persistence.py` | 13 |
| End-to-end | `test_e2e.py` | 8 |
| **Total** | | **93** |

E2E scenarios covered: happy path, HITL approve/reject/modify with replay, preemption + resume, replay determinism, capability exhaustion, validation error feedback.

---

## Tech Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+ |
| Package manager | `uv` |
| Data models | Pydantic V2 |
| Async runtime | `asyncio` |
| Persistence | SQLite + SQLAlchemy 2.0 ORM |
| Testing | pytest + pytest-asyncio |
| Linting | ruff (`E`, `F`, `I`, `N`, `UP` rules) |
| Build backend | hatchling |
| License | Apache 2.0 |

---

## Known Gaps & TODOs

| Item | Priority | Notes |
|------|----------|-------|
| Lodge context pager (M3) | High | Core subsystem; design docs exist, implementation not started |
| Sub-agent spawning | High | Data models ready (`child_checkpoint` in `SyscallRecord`); needs `spawn_agent` syscall handler |
| Agent return value storage | Medium | `AgentRunner` currently discards `await agent_fn(proxy)` result |
| CLI/API for HITL approval | Medium | Currently requires programmatic `HITLHandler` calls |
| Fan-out/fan-in (`spawn_agent_async`, `join_agent`) | Low | Depends on sub-agent spawning |
| Phase 2 Rust core (PyO3) | Future | Performance-critical paths; Python API surface preserved |

---

## Implementation Lessons

1. **Replay index advancement** — `SyscallProxy._replay_index` must advance after logging new syscalls, not only during replays. Missing this causes the next call to incorrectly enter the replay branch.

2. **SuspendInterrupt naming** — Intentionally not `SuspendError`; it's a control flow interrupt, not an error condition. Suppressed via `# noqa: N818`.

3. **HITL modification safety** — Never mutate `pending_hitl` arguments. Log the original request as `HITL_MODIFIED` and let the LLM re-plan. Mutating would break replay determinism.

4. **Preemption context** — `preemption_reason` and `preemption_payload` are metadata outside the `syscall_log`. They don't affect replay determinism.

5. **Fast/slow path split** — Eliminates the need for `asyncio.shield()`. Fast-path tools are idempotent; slow-path tools gate on HITL before execution.

---

## Build & Verify

```bash
uv sync                          # install dependencies
uv run pytest tests/ -v          # 93 tests, all passing
uv run ruff check --fix src/ tests/   # 0 lint errors
uv run ruff format src/ tests/   # auto-format
```
