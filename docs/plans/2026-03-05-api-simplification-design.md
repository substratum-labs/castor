# API Simplification Design — `Castor` Facade

**Date:** 2026-03-05
**Status:** Approved
**Strategy:** New facade layer + preserve low-level components for advanced users

## Problem

Current quickstart requires users to manually assemble 5+ objects:

```python
registry = ToolRegistry()
@castor_tool(..., registry=registry)
dam = CastorDam(registry)
cap_mgr = CapabilityManager()
checkpoint = AgentCheckpoint(pid=..., status="RUNNING", ...)
runner = AgentRunner(dam, cap_mgr)
cp = await runner.run(agent, checkpoint)
```

`ToolRegistry`, `CastorDam`, `CapabilityManager` are implementation details that users shouldn't need to touch for common use cases.

## Target Experience

```python
from castor import Castor, castor_tool

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result: {query}"]

kernel = Castor()
cp = await kernel.run(my_agent, budgets={"api": 50.0})
```

Three concepts to learn: `Castor`, `castor_tool`, `proxy.syscall`.

## Design

### 1. `Castor` Unified Kernel Class

```python
class Castor:
    def __init__(
        self,
        *,
        tools: list[Callable] | None = None,
        lodge: CastorLodge | None = None,
        agent_registry: AgentRegistry | None = None,
        store: str | CheckpointStore | None = None,
        # Advanced injection:
        dam: CastorDam | None = None,
        capability_manager: CapabilityManager | None = None,
    ): ...
```

**Tool collection** (two modes, both supported):
- **Explicit:** `Castor(tools=[search, delete_files])` — takes functions decorated with `@castor_tool`
- **Automatic:** `Castor()` — collects all tools from `default_registry`
- Explicit takes priority; if `tools=` is provided, only those tools are registered.

**Internal assembly:**
- If `dam` not provided: creates `ToolRegistry` from collected tools → wraps in `CastorDam`
- If `capability_manager` not provided: creates default `CapabilityManager()`
- If `store` is a string: creates `CheckpointStore(store)`

### 2. `kernel.run()` — Simplified Execution

```python
async def run(
    self,
    agent_fn: Callable[[SyscallProxy], Any],
    *,
    budgets: dict[str, float] | None = None,
    checkpoint: AgentCheckpoint | None = None,
    pid: str | None = None,
) -> AgentCheckpoint:
```

- `budgets` not provided → no budget enforcement (unlimited)
- `pid` not provided → auto-generate `f"{agent_fn.__name__}-{uuid4().hex[:8]}"`
- `checkpoint` provided → resume mode (after HITL approval)
- Returns `AgentCheckpoint` with `.status`, `.result`, `.pending_hitl`

### 3. HITL Methods on Kernel

```python
await kernel.approve(checkpoint)
await kernel.reject(checkpoint, reason="Too risky")
await kernel.modify(checkpoint, new_args={"paths": ["/tmp/safe.log"]})
```

Internally delegates to `HITLHandler` with the kernel's own `dam` and `capability_manager`. Users never need to import or instantiate `HITLHandler`.

Full HITL flow:
```python
cp = await kernel.run(my_agent, budgets={"api": 10.0, "disk": 5.0})
if cp.status == "SUSPENDED_FOR_HITL":
    await kernel.approve(cp)
    cp = await kernel.run(my_agent, checkpoint=cp)
```

### 4. `proxy` Calling Conventions (Three Styles)

All three are supported simultaneously. Docs recommend 4A for quickstart.

**4A: Dynamic attribute (recommended for quickstart)**
```python
results = await proxy.search(query="hello")
```
Implemented via `__getattr__` returning an async callable bound to tool name.

**4B: `syscall` with kwargs (backward-compatible)**
```python
results = await proxy.syscall("search", query="hello")
# also still accepts dict:
results = await proxy.syscall("search", {"query": "hello"})
```
`syscall` signature: `async def syscall(self, tool_name, args=None, /, **kwargs)`.

**4C: `call` with function reference (IDE-friendly)**
```python
results = await proxy.call(search, query="hello")
```
Extracts `tool_name` from `search._castor_metadata.tool_name`.

### 5. Checkpoint Persistence

```python
kernel = Castor(store="sqlite:///path/to/db")
cp = await kernel.run(my_agent, budgets={"api": 50.0})
await kernel.save(cp)
loaded = await kernel.load("agent-001")
```

No `store=` → no persistence (in-memory only).

### 6. Public API Exports

**Primary (quickstart):**
```python
from castor import Castor, castor_tool, SyscallProxy
```

**Secondary (still exported, for advanced users):**
```python
from castor import (
    AgentCheckpoint, SuspendInterrupt, SyscallRecord,
    Capability, SyscallRequest, SyscallResponse,
    HITLHandler, CheckpointStore,
    ToolRegistry, CastorDam, CapabilityManager,
    AgentRunner, CastorLodge,
    LLMSyscall, StreamingLLMSyscall,
    AgentRegistry, castor_agent,
)
```

## Compatibility

- All existing low-level APIs remain functional and importable
- Existing examples continue to work without modification
- New examples and README quickstart use the facade API
- `@castor_tool(registry=...)` parameter remains but is no longer documented in quickstart

## File Changes

| File | Change |
|------|--------|
| `src/castor/core.py` | **NEW** — `Castor` class implementation |
| `src/castor/stream/proxy.py` | Add `__getattr__` (4A), kwargs to `syscall` (4B), `call` method (4C) |
| `src/castor/__init__.py` | Add `Castor` to exports |
| `README.md` | Rewrite quickstart with facade API |
| `examples/quickstart.py` | Rewrite with facade API |
| `tests/test_facade.py` | **NEW** — tests for `Castor` facade |
