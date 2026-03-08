# Castor

**A secure microkernel for LLM Agents.**

Castor cages LLMs inside a deterministic execution engine with strongly-typed tool validation, capability-based security budgets, and preemptive human-in-the-loop (HITL) interrupts.

> **New to Castor?** Start with the [Getting Started Guide](https://substrate-lab.github.io/castor-docs/docs/getting-started/quickstart), read the [Whitepaper](https://substrate-lab.github.io/castor-docs/docs/whitepaper/), or visit the [Castor Docs Site](https://substrate-lab.github.io/castor-docs/) for conceptual documentation and blog.

## Quick Start

```python
from castor import Castor, SyscallProxy, castor_tool

@castor_tool(consumes="api", cost_per_use=1.0, destructive=True)
async def send_email(to: str, body: str) -> str:
    return f"Sent to {to}"

async def agent(proxy: SyscallProxy) -> str:
    result = await proxy.send_email(to="team@co.com", body="Hello")
    return f"Done: {result}"

kernel = Castor()
cp = await kernel.run(agent, budgets={"api": 10.0})
# cp.status == "SUSPENDED_FOR_HITL" (destructive tool requires approval)
```

## Key Features

- **Checkpoint/Replay** -- Crash recovery with zero tool re-execution. Agent state is a deterministic syscall log.
- **Capability Budgets** -- Per-resource spending limits with graceful degradation when exhausted.
- **Human-in-the-Loop** -- Suspend, approve/reject/modify, and resume with full replay safety.
- **Token-Level Preemption** -- Cancel streaming LLM calls mid-token with proportional billing.
- **Multi-Agent** -- Spawn child agents with delegated budgets and HITL propagation.

## Architecture

| Subsystem | Module | Purpose |
|-----------|--------|---------|
| **Gate** | `castor.gate` | Tool registry & Pydantic validation |
| **Scheduler** | `castor.scheduler` | Checkpoint/replay scheduler |
| **Capability** | `castor.capability` | Budget tracking & delegation |
| **MMU** | `castor.mmu` | Context window memory management |

## Documentation

- [API Reference](api/index.md) -- Complete class and method reference
