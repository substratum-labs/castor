# Contributing to Castor

Thank you for your interest in contributing to Castor!

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

## Setup

```bash
git clone https://github.com/substrate-lab/castor.git
cd castor
uv sync
```

## Development Commands

```bash
uv run pytest tests/ -v        # Run tests
uv run ruff check src/ tests/  # Lint
uv run ruff format src/ tests/ # Format
```

## Architecture Overview

Castor is a microkernel with four subsystems:

| Module | Purpose |
|--------|---------|
| `dam/` | Tool registry and Pydantic validation |
| `stream/` | Checkpoint/replay scheduler, HITL, persistence |
| `lodge/` | Context window memory management |
| `capability/` | Budget tracking and delegation |

All side effects go through `SyscallProxy.syscall()` — never bypass it. Agent state is a replay journal (`syscall_log`). Suspend raises `SuspendInterrupt` to unwind the stack; resume replays from the top with cached responses.

## Pull Request Expectations

- All tests pass (`uv run pytest tests/ -v`)
- Zero lint errors (`uv run ruff check src/ tests/`)
- Code is formatted (`uv run ruff format --check src/ tests/`)
- Describe what changed and why in the PR description
- Add tests for new functionality

## Code Conventions

- All data models are Pydantic `BaseModel` subclasses
- Agent functions: `async def agent_name(proxy: SyscallProxy) -> Any`
- Tools: `@castor_tool(consumes=..., cost_per_use=..., destructive=...)`
- Use `dict[str, Any]` not `Dict[str, Any]` (Python 3.11+ builtins)
- Never throw raw exceptions to user space — wrap in `SyscallResponse`
