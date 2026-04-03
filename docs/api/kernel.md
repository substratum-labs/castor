# Castor Kernel

The unified kernel facade — assembles all subsystems behind a single object.

## Quick Usage

```python
from castor import Castor, auto_approve
from castor.lib import tool

async def search(query: str) -> list[str]:
    return ["result1", "result2"]

async def delete_file(path: str) -> str:
    return f"Deleted {path}"

kernel = Castor(
    tools=[search, delete_file],
    destructive=["delete_file"],
    budgets={"api": 50},
)
cp = await kernel.run(my_agent)
```

## Constructor

The `Castor()` constructor accepts several configuration styles:

| Parameter | Description |
|-----------|-------------|
| `tools=[...]` | List of callables, `@castor_tool` decorated functions, or `(name, func)` tuples |
| `destructive=[...]` | Tool names that require HITL approval |
| `budgets={...}` | Default resource budgets (alias for `default_budgets`) |
| `llm=callable` | Auto-wrap an LLM callable as a tracked syscall |
| `llm_cost`, `llm_resource` | Cost and resource type for the LLM syscall |
| `roche=True` | Enable Roche sandbox integration (requires `roche-sandbox[castor]`) |
| `gate=` | Advanced: pass a pre-built `SyscallGate` |
| `capability_manager=` | Advanced: pass a pre-built `CapabilityManager` |
| `store="sqlite:///path"` | SQLite persistence for checkpoints |
| `structured_results=True` | Return `SyscallResult` instead of raw values |

## Castor

::: castor.core.Castor
    options:
      members:
        - __init__
        - run
        - run_until_complete
        - run_async
        - scan
        - approve
        - reject
        - modify
        - save
        - load
        - preempt

## ExecutionSummary

Returned by `kernel.scan(cp)` after speculative execution.

::: castor.kernel.summary.ExecutionSummary

::: castor.kernel.summary.FlaggedStep

## CastorTask

::: castor.core.CastorTask
