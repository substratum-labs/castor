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
| `gate/` | Tool registry & Pydantic validation (SyscallGate) |
| `scheduler/` | Checkpoint/replay scheduler |
| `mmu/` | Context window memory management (MMU) |
| `capability/` | Budget tracking & delegation (Capability Manager) |
| `models/` | Shared Pydantic data models |
| `mcp/` | MCP server — expose @castor_tool as MCP tools via FastMCP |

## Key Design Decisions

- **Checkpoint/Replay model** (not coroutine serialization). Agent state is a `syscall_log` replay journal. Suspend = raise `SuspendInterrupt` to unwind the stack. Resume = replay function from top, serve cached responses.
- **SyscallProxy** is the only interface between agent functions and the kernel. All side effects go through `await proxy.syscall(tool_name, args)`.
- **HITL with modification** logs the original request as `HITL_MODIFIED` and lets the LLM re-plan. Never mutate `pending_hitl` arguments directly — this would break replay determinism.
- **Sub-agent spawning** creates nested `AgentCheckpoint` objects. The child's result is cached in the parent's syscall log.

## Code Conventions

- All data models must be Pydantic `BaseModel` subclasses
- Use `Literal` types for status enums in models
- Agent functions signature: `async def agent_name(proxy: SyscallProxy) -> Any`
- Tools are registered with `@castor_tool(consumes=..., cost_per_use=..., destructive=...)` or `ToolMetadata.from_function(fn, ...)`
- Never throw raw Python exceptions to user space — wrap in `SyscallResponse` with feedback
- Prefer `dict[str, Any]` over `Dict[str, Any]` (Python 3.11+ builtins)
- Missing resource type in capabilities = not tracked = unlimited (`deduct()` no-ops, `check()` returns True)
- Gate skips Pydantic validation for tools with `input_schema={}` (LLM wrappers pass through)
- Access kernel internals via public properties: `kernel.gate`, `kernel.capability_manager`, `kernel.store`

## Sister Projects

- **Tiphys** (`../tiphys/`) — Digital Life Form agent built on Castor. Drives Castor feature priorities.
- **Roche** (`../roche/`) — Universal sandbox orchestrator (Rust). Castor does not depend on Roche; Tiphys bridges them.
- **Pollux** — Embodied agent OS (future). Twin star to Castor — shared kernel, different scheduler.
- **castor-internal** (`../castor-internal/`) — Private design docs, plans, cross-project status.
  - Read `status/PROGRESS.md` for current cross-project state.
  - Read `design/TIPHYS_VISION.md` for the Digital Life Form vision.
  - Read `design/CASTORD_ARCHITECTURE.md` for the Ring model and structural prep status.

## Documentation

- `castor` (this repo) — Source code + `docs/` for MkDocs API reference (mkdocstrings)
- `../castor-docs/` — Docusaurus site (whitepaper, blog, architecture guides)
