# Castor

<p align="center">
  <img src="assets/logo.png" alt="Castor Logo" width="300" />
</p>

[![CI](https://github.com/substratum-labs/castor/actions/workflows/ci.yml/badge.svg)](https://github.com/substratum-labs/castor/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> **Alpha Release** — Castor is in active development. Kernel primitives and the SyscallProxy API may undergo breaking changes before 1.0.

A secure microkernel for LLM Agents. Castor cages LLMs inside a deterministic execution engine with strongly-typed tool validation, capability-based security budgets, and preemptive human-in-the-loop (HITL) interrupts.

Castor is a **kernel**, not an agent framework. It provides core primitives that agent frameworks and custom agents integrate with. Your agent brings its own LLM client — Castor controls the side effects.

## Key Features

- **Checkpoint/Replay** — Agent state is a replay log, not serialized coroutines. Suspend anywhere, resume later, even in a different process.
- **Capability-Based Security** — Budget-tracked permissions. A child agent can never exceed its parent's budget.
- **Human-in-the-Loop** — Destructive tools suspend execution for human approval. Approve, reject, or modify with natural language feedback.
- **Preemptive Scheduling** — `asyncio.Task.cancel()` + checkpoint/replay = true preemption with zero agent complexity.
- **Strongly-Typed Tools** — `@castor_tool` auto-generates Pydantic schemas. Validation errors become LLM-readable feedback, not crashes.

<p align="center">
  <img src="assets/demo.gif" alt="Castor Demo — HITL modify flow" width="800">
  <br>
  <em>An agent suspends at a destructive call. Human modifies. Kernel replays and resumes.</em>
</p>

## Quick Start

```bash
pip install castor-kernel
# or with uv
uv add castor-kernel
```

### 1. Write an agent file

```python
# agent.py — this is ALL you need
from castor import castor_tool
from castor.lib import tool, budget

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

async def agent() -> str:
    results = await tool("search", query="hello")
    return f"Found: {results} ({budget('api')} budget left)"
```

### 2. Run it

```bash
castor run agent.py --budget api=50
```

That's it. The CLI auto-discovers tools, creates the kernel, enforces budgets, and handles HITL. No boilerplate.

### Already have an agent? Use the MCP server

Add Castor as a guard layer to **any** MCP-compatible agent (Claude, Cursor, etc.) — zero code changes to your agent:

```python
# tools.py — define your guarded tools
from castor import castor_tool

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

@castor_tool(consumes="disk", destructive=True)
def delete_files(paths: list[str]) -> int:
    return len(paths)
```

```bash
# Start the MCP server
castor-mcp --tools-module tools
```

Or add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "castor": {
      "command": "castor-mcp",
      "args": ["--tools-module", "tools"]
    }
  }
}
```

Your agent gets budget limits and HITL approval on destructive calls — automatically. The agent calls `castor_init(budgets={"api": 50})` once, then uses tools normally. Destructive calls return `pending_approval` for human review.

<details>
<summary>Programmatic API — for embedding in your own application</summary>

If you're building a framework or need full control, use the Python API directly:

```python
# castor.lib style — agent code has zero kernel imports
import asyncio
from castor import Castor, castor_tool
from castor.lib import tool, budget

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

async def my_agent() -> str:
    results = await tool("search", query="hello")
    return f"Found: {results} ({budget('api')} budget left)"

async def main():
    kernel = Castor(tools=[search])
    cp = await kernel.run(my_agent, budgets={"api": 50.0})
    print(cp.result)

if __name__ == "__main__":
    asyncio.run(main())
```

```python
# SyscallProxy style — direct proxy for streaming, preemption, sub-agents
import asyncio
from castor import Castor, castor_tool, SyscallProxy

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

async def my_agent(proxy: SyscallProxy) -> str:
    results = await proxy.search(query="hello")
    return f"Found: {results}"

async def main():
    kernel = Castor(tools=[search])
    cp = await kernel.run(my_agent, budgets={"api": 50.0})
    print(cp.result)

if __name__ == "__main__":
    asyncio.run(main())
```

</details>

See [examples/](examples/) for runnable demos covering HITL, budgets, preemption, multi-agent, and high-level patterns.

<details>
<summary>Full example with HITL and destructive tools</summary>

```python
import asyncio
from castor import Castor, castor_tool, SyscallProxy

@castor_tool(consumes="api", cost_per_use=1.0)
async def web_search(query: str) -> list[str]:
    return [f"Result for: {query}"]

@castor_tool(consumes="disk", destructive=True, requires_hitl=True)
def delete_files(paths: list[str]) -> int:
    return len(paths)  # actual deletion logic here

kernel = Castor(tools=[web_search, delete_files])

async def my_agent(proxy: SyscallProxy) -> str:
    results = await proxy.web_search(query="climate data")
    # This will suspend for human approval:
    deleted = await proxy.delete_files(paths=["/tmp/old"])
    return f"Found {results}, deleted {deleted} files"

async def main():
    cp = await kernel.run(my_agent, budgets={"api": 100.0, "disk": 10.0})
    print(f"Status: {cp.status}")  # SUSPENDED

    # Human approves, then resume:
    await kernel.approve(cp)
    cp = await kernel.run(my_agent, checkpoint=cp)
    print(f"Result: {cp.result}")

asyncio.run(main())
```

</details>

## Architecture

Castor has four kernel subsystems:

| Module | Purpose |
|--------|---------|
| **Gate** (`castor.gate`) | Tool registry & Pydantic validation |
| **Scheduler** (`castor.scheduler`) | Checkpoint/replay scheduler, HITL, preemption |
| **Capability** (`castor.capability`) | Budget tracking & delegation |
| **MMU** (`castor.mmu`) | Context window memory management |

Plus a standard library for agent developers:

| Module | Purpose |
|--------|---------|
| **Lib** (`castor.lib`) | Primitives (`tool`, `chat`, `spawn`) and patterns (`react`, `parallel`, `supervisor`) |
| **CLI** (`castor.cli`) | `castor run`, `castor ps`, `castor inspect`, HITL commands |
| **MCP** (`castor.mcp`) | Expose `@castor_tool` as MCP tools via FastMCP |

All side effects go through the syscall interface. The kernel decides: replay from cache, execute (fast path), or suspend for human approval (slow path).

## Security Scope

Castor provides **application-layer capability control** — it intercepts and gates every tool call an LLM agent makes, enforcing budgets and human approval on destructive operations. It does **not** provide OS-level sandboxing (process isolation, filesystem jails, network firewalls). For defense-in-depth, run Castor inside a container or VM sandbox. Castor controls what the agent *intends* to do; your infrastructure controls what it *can* do.

## Documentation

- **[API Reference](https://substratum-labs.github.io/castor/)** — MkDocs-generated reference for all modules
- **[Architecture & Guides](https://substratum-labs.github.io/castor-docs/)** — Whitepaper, architecture deep dives, getting started guides

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/substratum-labs/castor.git
cd castor
uv sync
uv run pytest          # 373 tests
uv run ruff check src/ # lint
uv run ruff format src/ # format
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
