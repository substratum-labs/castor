# Castor

<p align="center">
  <img src="assets/logo.png" alt="Castor Logo" width="300" />
</p>

A secure microkernel for LLM Agents. Castor cages LLMs inside a deterministic execution engine with strongly-typed tool validation, capability-based security budgets, and preemptive human-in-the-loop (HITL) interrupts.

Castor is a **kernel**, not an agent framework. It provides core primitives that agent frameworks and custom agents integrate with. Your agent brings its own LLM client — Castor controls the side effects.

## Key Features

- **Checkpoint/Replay** — Agent state is a replay log, not serialized coroutines. Suspend anywhere, resume later, even in a different process.
- **Capability-Based Security** — Budget-tracked permissions. A child agent can never exceed its parent's budget.
- **Human-in-the-Loop** — Destructive tools suspend execution for human approval. Approve, reject, or modify with natural language feedback.
- **Preemptive Scheduling** — `asyncio.Task.cancel()` + checkpoint/replay = true preemption with zero agent complexity.
- **Strongly-Typed Tools** — `@castor_tool` auto-generates Pydantic schemas. Validation errors become LLM-readable feedback, not crashes.

## Quick Start

```bash
pip install castor
# or with uv
uv add castor
```

```python
import asyncio
from castor import (
    castor_tool, CastorDam, CapabilityManager, AgentRunner,
    AgentCheckpoint, SyscallProxy, ToolRegistry,
)

# 1. Register tools
registry = ToolRegistry()

@castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
async def web_search(query: str) -> list[str]:
    return [f"Result for: {query}"]

@castor_tool(consumes="disk", destructive=True, requires_hitl=True, registry=registry)
def delete_files(paths: list[str]) -> int:
    return len(paths)  # actual deletion logic here

# 2. Set up kernel
dam = CastorDam(registry)
cap_mgr = CapabilityManager()

# 3. Define an agent function
async def my_agent(proxy: SyscallProxy) -> str:
    results = await proxy.syscall("web_search", {"query": "climate data"})
    # This will suspend for human approval:
    deleted = await proxy.syscall("delete_files", {"paths": ["/tmp/old"]})
    return f"Found {results}, deleted {deleted} files"

# 4. Run it
async def main():
    caps = cap_mgr.create_capabilities({"api": 100.0, "disk": 10.0})
    checkpoint = AgentCheckpoint(
        pid="agent-001",
        status="RUNNING",
        agent_function_name="my_agent",
        capabilities=caps,
    )
    runner = AgentRunner(dam, cap_mgr)
    result = await runner.run(my_agent, checkpoint)
    print(f"Status: {result.status}")  # SUSPENDED_FOR_HITL

asyncio.run(main())
```

## Architecture

Castor has four kernel subsystems:

| Module | Purpose | Status |
|--------|---------|--------|
| **Dam** (`castor.dam`) | Tool registry & Pydantic validation | Complete |
| **Stream** (`castor.stream`) | Checkpoint/replay scheduler, HITL, preemption | Complete |
| **Capability** (`castor.capability`) | Budget tracking & delegation | Complete |
| **Lodge** (`castor.lodge`) | Context window memory management | Planned |

All side effects go through `await proxy.syscall(tool_name, args)`. The kernel decides: replay from cache, execute (fast path), or suspend for human approval (slow path).

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/substrate-lab/castor.git
cd castor
uv sync
uv run pytest          # 90 tests
uv run ruff check src/ # lint
uv run ruff format src/ # format
```

## Design Documents

- [PRD](docs/PRD.md) — Product requirements
- [ADD](docs/ADD.md) — Architecture design
- [DDD](docs/DDD.md) — Detailed design (canonical source of truth)
- [Checkpoint/Replay](docs/CHECKPOINT_REPLAY.md) — Coroutine serialization deep dive
- [Preemptive Scheduling](docs/PREEMPTIVE_SCHEDULING.md) — How `task.cancel()` + replay = true preemption
- [Roadmap](docs/ROADMAP.md) — Implementation status and future plans

## License

Apache 2.0 — see [LICENSE](LICENSE).
