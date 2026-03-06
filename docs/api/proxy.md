# SyscallProxy

The gateway between agent code and the kernel. **All side effects must go through the proxy** to ensure deterministic checkpoint/replay.

## Usage Patterns

```python
async def my_agent(proxy: SyscallProxy) -> str:
    # Pattern 1: Dynamic attribute access
    result = await proxy.web_search(query="hello")

    # Pattern 2: Explicit syscall
    result = await proxy.syscall("web_search", query="hello")

    # Pattern 3: Function reference
    result = await proxy.call(web_search, query="hello")

    # Check budget
    remaining = proxy.budget("api")

    return "done"
```

## SyscallProxy

::: castor.stream.proxy.SyscallProxy
    options:
      members:
        - syscall
        - call
        - budget
        - spawn
        - spawn_sync
        - join
        - preemption_context
        - clear_preemption_context
        - is_replaying
        - charge_partial
