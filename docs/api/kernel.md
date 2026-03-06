# Castor Kernel

The unified kernel facade — assembles all subsystems behind a single object.

## Quick Usage

```python
from castor import Castor, castor_tool, SyscallProxy

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return ["result1", "result2"]

kernel = Castor()
cp = await kernel.run(my_agent, budgets={"api": 50.0})
```

## Castor

::: castor.core.Castor
    options:
      members:
        - __init__
        - run
        - run_until_complete
        - run_async
        - approve
        - reject
        - modify
        - save
        - load
        - preempt

## CastorTask

::: castor.core.CastorTask
