# Agent Registry

Register and spawn sub-agents with delegated budgets and HITL propagation.

!!! warning "Experimental"
    The Agent Registry is marked **experimental** — its API may change between minor versions.

## Registering Agents

```python
from castor import castor_agent, SyscallProxy

@castor_agent(name="researcher")
async def researcher(proxy: SyscallProxy) -> str:
    results = await proxy.web_search(query="latest findings")
    return f"Found {len(results)} results"
```

## Spawning from a Coordinator

```python
async def coordinator(proxy: SyscallProxy) -> str:
    # Async spawn (non-blocking)
    handle = await proxy.spawn("researcher", capabilities={"api": 10.0})
    # ... do other work ...
    result = await proxy.join(handle)

    # Sync spawn (blocking)
    result = await proxy.spawn_sync("researcher", capabilities={"api": 10.0})

    return f"Research: {result}"
```

## AgentRegistry

::: castor.stream.agent_registry.AgentRegistry

## castor_agent

::: castor.stream.agent_registry.castor_agent
