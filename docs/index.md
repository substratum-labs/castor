# Castor

**A secure microkernel for LLM Agents.**

Castor cages LLMs inside a deterministic execution engine with strongly-typed tool validation, capability-based security budgets, and preemptive human-in-the-loop (HITL) interrupts.

> **New to Castor?** Start with the [Getting Started Guide](https://substratum-labs.github.io/castor-docs/docs/getting-started/quickstart), read the [Whitepaper](https://substratum-labs.github.io/castor-docs/docs/whitepaper/), or visit the [Castor Docs Site](https://substratum-labs.github.io/castor-docs/) for conceptual documentation and blog.

## Quick Start

```bash
pip install castor-kernel
```

```python
import asyncio
from castor import Castor, auto_approve
from castor.lib import tool

# Your existing tools -- plain functions, no decorators needed
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

async def delete_file(path: str) -> str:
    return f"Deleted {path}"

# Your agent -- doesn't know about Castor
async def my_agent():
    results = await tool("search", query="old logs")
    await tool("delete_file", path="/tmp/old1")
    return "Cleaned up"

async def main():
    kernel = Castor(
        tools=[search, delete_file],
        destructive=["delete_file"],       # mark dangerous tools
        budgets={"api": 10},
    )

    # HITL mode -- destructive tools pause for approval
    cp = await kernel.run(my_agent)

    # Speculative mode -- full speed, review after
    cp = await kernel.run(my_agent, speculative=True)
    summary = kernel.scan(cp)
    print(f"{summary.total_steps} steps, {summary.flagged_count} need review")

asyncio.run(main())
```

## Key Features

- **Three Security Levels** -- HITL (human approves every action), Speculative (full speed + post-hoc review), Time-Travel (rewind and fix mistakes).
- **Checkpoint/Replay** -- Crash recovery with zero tool re-execution. Agent state is a deterministic syscall log.
- **Capability Budgets** -- Per-resource spending limits with graceful degradation when exhausted.
- **Human-in-the-Loop** -- Suspend, approve/reject/modify, and resume with full replay safety.
- **Speculative Execution** -- Agent runs without interruption; destructive ops flagged for review.
- **Time-Travel** -- `cp.fork(at_step=5)` rewinds to step 5; cached steps cost nothing.
- **Token-Level Preemption** -- Cancel streaming LLM calls mid-token with proportional billing.
- **Multi-Agent** -- Spawn child agents with delegated budgets and HITL propagation.

## Architecture

| Subsystem | Module | Purpose |
|-----------|--------|---------|
| **Gate** | `castor.gate` | Tool registry & Pydantic validation |
| **Scheduler** | `castor.scheduler` | Checkpoint/replay scheduler |
| **Capability** | `castor.capability` | Budget tracking & delegation |
| **MMU** | `castor.mmu` | Context window memory management |
| **Lib** | `castor.lib` | Agent developer standard library |
| **CLI** | `castor.cli` | Command-line interface |

## Three Levels of Protection

| Level | Mode | How it works |
|-------|------|--------------|
| **L1** | HITL | `kernel.run(agent)` -- destructive tools pause for human approval |
| **L2** | Speculative | `kernel.run(agent, speculative=True)` -- full speed, `kernel.scan(cp)` for post-hoc review |
| **L3** | Time-Travel | `cp.fork(at_step=5)` -- rewind to mistake, re-execute from there |

## Two APIs

Castor provides two API styles:

**castor.lib (Agent Developer)** -- No proxy, no kernel imports:

```python
from castor.lib import tool, chat, spawn, join

async def my_agent() -> str:
    results = await tool("web_search", query="castor")
    summary = await chat(f"Summarize: {results}")
    return summary
```

**SyscallProxy (Classic)** -- Explicit proxy parameter:

```python
from castor import SyscallProxy

async def my_agent(proxy: SyscallProxy) -> str:
    results = await proxy.web_search(query="castor")
    return str(results)
```

## Documentation

- [API Reference](api/index.md) -- Complete class and method reference
- [castor.lib](api/lib.md) -- Agent developer standard library
- [CLI Reference](api/cli.md) -- Command-line usage
