# Phase B: castor.lib — Core Subset Design

> Date: 2026-03-08
> Status: Approved
> Scope: Core subset only. Advanced patterns (parallel, react, map_reduce, run_task) deferred.

## Decisions

| Question | Decision |
|----------|----------|
| Scope | Core subset first, advanced patterns later |
| `parallel()` | Deferred to advanced phase |
| Signature detection | `inspect.signature` auto-detect (0 params = new, 1+ = legacy) |
| `chat()` tool_name | Configurable with default `"llm_inference"` |
| Module structure | `lib/` package (方案 B) |
| `try_tool` | Keep as semantic alias for `tool` |
| ContextVar for legacy | Always set — enables gradual migration |
| Top-level export | No — `castor.lib` stays separate from `castor.__init__` |

## Module Structure

```
src/castor/lib/
├── __init__.py      # re-export: tool, chat, budget, spawn, join, try_tool
├── _context.py      # ContextVar[SyscallProxy], get/set helpers
├── primitives.py    # tool(), chat(), budget(), try_tool()
└── spawn.py         # spawn(), join()
```

## ContextVar Bridge (`_context.py`)

```python
from contextvars import ContextVar
from castor.scheduler.proxy import SyscallProxy

_proxy_var: ContextVar[SyscallProxy] = ContextVar("castor_proxy")

def get_proxy() -> SyscallProxy:
    """Get current proxy. Raises RuntimeError outside Castor.run()."""
    try:
        return _proxy_var.get()
    except LookupError:
        raise RuntimeError(
            "castor.lib functions must be called inside Castor.run()"
        )

def set_proxy(proxy: SyscallProxy) -> None:
    _proxy_var.set(proxy)
```

## Primitives (`primitives.py`)

```python
async def tool(name: str, /, **kwargs) -> Any:
    """Call a registered tool."""
    return await get_proxy().syscall(name, **kwargs)

async def chat(prompt: str, *, system: str = "", tool_name: str = "llm_inference") -> str:
    """Call an LLM tool."""
    return await get_proxy().syscall(tool_name, prompt=prompt, system=system)

def budget(resource: str) -> float:
    """Remaining budget for a resource type."""
    return get_proxy().budget(resource)

async def try_tool(name: str, /, **kwargs) -> Any:
    """Semantic alias for tool() — communicates intent that failure is expected."""
    return await get_proxy().syscall(name, **kwargs)
```

## Spawn (`spawn.py`)

```python
async def spawn(agent_name: str, *, capabilities: dict[str, float] | None = None) -> str:
    """Spawn child agent async, return join handle."""
    return await get_proxy().spawn(agent_name, capabilities=capabilities)

async def join(handle: str) -> Any:
    """Wait for spawned child to complete."""
    return await get_proxy().join(handle)
```

## `Castor.run()` Changes

1. After creating proxy, call `set_proxy(proxy)` — **always**, even for legacy agents
2. Detect agent signature via `inspect.signature`:
   - 0 required params → `await agent_fn()` (new-style)
   - 1+ required params → `await agent_fn(proxy)` (legacy)
3. ContextVar is set **before** agent invocation, cleared after

Key: setting ContextVar for legacy agents enables gradual migration — legacy agents' sub-functions can use `castor.lib`.

## Test Plan

| Test file | Coverage |
|-----------|----------|
| `test_context.py` | ContextVar set/get/RuntimeError outside run |
| `test_primitives.py` | tool/chat/budget/try_tool via ContextVar |
| `test_spawn_lib.py` | spawn/join via ContextVar |
| `test_signature_detection.py` | Castor.run() dual-signature detection |
| `test_integration_lib.py` | End-to-end: new-style agent + Castor.run() |

## Constraints (from API_REFACTOR.md §12.6)

- All `castor.lib` function signatures use primitive types only (str, int, float, dict, list, bool, None)
- No Python-specific types in the API surface — this becomes the cross-language POSIX standard
