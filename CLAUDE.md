# Castor — Claude Code Guidelines

## Project Overview

Castor is a secure microkernel for LLM Agents. It cages LLMs inside a deterministic execution engine with strongly-typed tool validation, capability-based security budgets, and preemptive human-in-the-loop (HITL) interrupts.

**Phase 1:** Python prototype. **Phase 2:** Rust core via PyO3.

## Tech Stack

- Python 3.11+, managed with `uv`
- Pydantic V2 for all data models and validation
- asyncio for concurrency
- SQLAlchemy + SQLite for state persistence
- pytest + pytest-asyncio for testing
- ruff for linting

## Commands

```bash
uv sync                  # Install/update dependencies
uv run pytest            # Run tests
uv run ruff check src/   # Lint
uv run ruff format src/  # Format
```

## Architecture

Four kernel subsystems, all in `src/castor/`:

| Module | Purpose |
|---|---|
| `dam/` | Tool registry & Pydantic validation (Castor Dam) |
| `stream/` | Checkpoint/replay scheduler (Castor Stream) |
| `lodge/` | Context window memory management (Castor Lodge) |
| `capability/` | Budget tracking & delegation (Capability Manager) |
| `models/` | Shared Pydantic data models |

## Key Design Decisions

- **Checkpoint/Replay model** (not coroutine serialization). Agent state is a `syscall_log` replay journal. Suspend = raise `SuspendInterrupt` to unwind the stack. Resume = replay function from top, serve cached responses.
- **SyscallProxy** is the only interface between agent functions and the kernel. All side effects go through `await proxy.syscall(tool_name, args)`.
- **HITL with modification** logs the original request as `HITL_MODIFIED` and lets the LLM re-plan. Never mutate `pending_hitl` arguments directly — this would break replay determinism.
- **Sub-agent spawning** creates nested `AgentCheckpoint` objects. The child's result is cached in the parent's syscall log.

## Code Conventions

- All data models must be Pydantic `BaseModel` subclasses
- Use `Literal` types for status enums in models
- Agent functions signature: `async def agent_name(proxy: SyscallProxy) -> Any`
- Tools are registered with `@castor_tool(consumes=..., cost_per_use=..., destructive=...)`
- Never throw raw Python exceptions to user space — wrap in `SyscallResponse` with feedback
- Prefer `dict[str, Any]` over `Dict[str, Any]` (Python 3.11+ builtins)

## Design Documents

Detailed architecture and design rationale live in `docs/`:
- `docs/PRD.md` — Product requirements
- `docs/ADD.md` — Architecture design
- `docs/DDD.md` — Detailed design (canonical source of truth for data models)
- `docs/DESIGN_REVIEW.md` — Review notes and open questions
