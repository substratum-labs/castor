# Castor Integration API Improvements

Based on Chela/OpenClaw integration feedback. Prioritized by impact/effort ratio.

## Phase A: Quick Wins (low effort, high impact)

### A1. Budget skip on missing resource type

**Problem**: `CapabilityManager.deduct()` raises `CapabilityExhaustedError` when a resource type is not in the capabilities dict. This means `budgets=None` (→ `caps={}`) causes every cost-bearing tool to fail with "budget exhausted".

**Fix**: Change `deduct()` to no-op (return early) when the resource type is not in capabilities. Aligns with `refund()` which already silently no-ops on missing resources.

```python
# capability/manager.py — deduct()
def deduct(self, capabilities, resource_type, cost):
    cap = capabilities.get(resource_type)
    if cap is None:
        return  # Not tracked → no enforcement
    remaining = cap.max_budget - cap.current_usage
    if remaining < cost:
        raise CapabilityExhaustedError(resource_type, cost, remaining)
    cap.current_usage += cost
```

Also fix `check()` to return `True` (not `False`) when resource is missing — "not tracked" = "allowed".

**Files**: `src/castor/capability/manager.py`
**Tests**: Update existing tests, add `test_deduct_missing_resource_noop`, `test_check_missing_resource_returns_true`

---

### A2. ToolMetadata.from_function() classmethod

**Problem**: Creating `ToolMetadata` from a plain function requires manually extracting name, schema, async flag, annotations.

**Fix**: Add `ToolMetadata.from_function(fn, **overrides)` that reuses `_generate_schema()` logic.

```python
# dam/registry.py
class ToolMetadata(BaseModel):
    @classmethod
    def from_function(
        cls,
        func: Callable,
        *,
        consumes: str = "_default",
        cost_per_use: float = 0.0,
        requires_hitl: bool = False,
        destructive: bool = False,
        timeout_seconds: float | None = None,
    ) -> ToolMetadata:
        from castor.gate.decorator import _generate_schema
        return cls(
            tool_name=func.__name__,
            consumes=consumes,
            cost_per_use=cost_per_use,
            requires_hitl=requires_hitl,
            destructive=destructive,
            input_schema=_generate_schema(func),
            func=func,
            is_async=asyncio.iscoroutinefunction(func),
            timeout_seconds=timeout_seconds,
        )
```

**Files**: `src/castor/dam/registry.py`
**Tests**: `test_tool_metadata_from_function`, `test_from_function_async_detection`, `test_from_function_with_overrides`

---

### A3. MemoryCheckpointStore + CheckpointStore Protocol

**Problem**: `CheckpointStore` is SQLite-only with no abstract interface. Testing requires skipping persistence entirely.

**Fix**:
1. Define `CheckpointStoreProtocol` (runtime_checkable Protocol)
2. Add `MemoryCheckpointStore` (dict-backed, no dependencies)
3. Keep existing SQLite impl as `SqliteCheckpointStore` (alias `CheckpointStore` for backward compat)

```python
# stream/persistence.py
@runtime_checkable
class CheckpointStoreProtocol(Protocol):
    def save(self, checkpoint: AgentCheckpoint) -> None: ...
    def load(self, pid: str) -> AgentCheckpoint: ...
    def delete(self, pid: str) -> None: ...
    def list_pids(self) -> list[str]: ...

class MemoryCheckpointStore:
    """In-memory checkpoint store for testing."""
    def __init__(self):
        self._store: dict[str, AgentCheckpoint] = {}
    ...
```

**Files**: `src/castor/stream/persistence.py`
**Tests**: `test_memory_checkpoint_store_crud`, existing SQLite tests unchanged
**Exports**: Add `MemoryCheckpointStore`, `CheckpointStoreProtocol` to `__init__.py`

---

## Phase B: Medium Effort Improvements

### B1. Expose kernel internals as public read-only properties

**Problem**: Guard layer integrations need access to `dam` and `cap_mgr` but they're private (`_dam`, `_cap_mgr`).

**Fix**: Add public properties on `Castor`:

```python
@property
def gate(self) -> SyscallGate:
    return self._gate

@property
def capability_manager(self) -> CapabilityManager:
    return self._cap_mgr

@property
def store(self) -> CheckpointStore | None:
    return self._store
```

**Files**: `src/castor/core.py`
**Tests**: `test_kernel_dam_property`, `test_kernel_cap_mgr_property`

---

### B2. Default budgets on Castor()

**Problem**: Every `kernel.run()` call must pass `budgets=`. No way to set org-wide defaults.

**Fix**: Add `default_budgets` parameter to `Castor()`. Used when `run(budgets=None)`.

```python
class Castor:
    def __init__(self, *, default_budgets: dict[str, float] | None = None, ...):
        self._default_budgets = default_budgets

    async def run(self, agent_fn, *, budgets=None, ...):
        if checkpoint is None:
            effective_budgets = budgets if budgets is not None else self._default_budgets
            if effective_budgets is not None:
                caps = self._cap_mgr.create_capabilities(effective_budgets)
            else:
                caps = {}
```

**Files**: `src/castor/core.py`
**Tests**: `test_default_budgets_used_when_none`, `test_explicit_budgets_override_default`

---

### B3. Enrich on_hitl callback signature

**Problem**: `on_hitl(cp)` requires extracting `cp.pending_tool` / `cp.pending_args` manually.

**Fix**: Keep backward compat, add structured info. Change callback to receive a `HITLEvent` dataclass:

```python
@dataclass
class HITLEvent:
    checkpoint: AgentCheckpoint
    tool_name: str
    arguments: dict[str, Any]

# In run_until_complete:
event = HITLEvent(
    checkpoint=cp,
    tool_name=cp.pending_tool,
    arguments=cp.pending_args or {},
)
decision, feedback = await on_hitl(event)
```

For backward compat, also accept `(cp) -> (decision, feedback)` via inspect check, but prefer the new signature.

Actually — simpler: just define `HITLEvent` and pass it. Since `on_hitl` is user-provided, the user controls the signature. No backward compat needed if we document the change.

**Wait — better approach**: Keep `on_hitl(cp)` since `cp.pending_tool` and `cp.pending_args` already exist as properties. Just improve documentation. The "event" approach adds a new type for marginal benefit. **Downgrade to documentation fix only.**

---

### B4. LLM schema validation skip

**Problem**: Dam validates LLM tool arguments against auto-generated schema, which can fail on complex signatures.

**Fix**: Dam's `validate()` should skip validation when `input_schema == {}` (empty schema = no validation).

```python
# dam/validator.py — validate()
def validate(self, tool_name, arguments):
    meta = self._registry.get(tool_name)
    if not meta.input_schema or meta.input_schema == {}:
        return arguments  # No schema → pass through
    # ... existing Pydantic validation
```

**Files**: `src/castor/dam/validator.py`
**Tests**: `test_validate_empty_schema_passthrough`

---

## Phase C: Nice-to-Have (larger changes, lower priority)

### C1. proxy.llm_call() sugar

Add convenience method on SyscallProxy for LLM calls without needing to create LLMSyscall objects.

```python
async def llm_call(self, call_fn, /, **kwargs):
    """Issue an LLM call through the syscall path.

    Automatically wraps call_fn as a transient tool.
    """
    tool_name = f"_llm_{id(call_fn)}"
    # Register transiently if not already registered
    if not self._dam.registry.has_tool(tool_name):
        meta = ToolMetadata.from_function(call_fn, consumes="api_usd", cost_per_use=1.0)
        meta.tool_name = tool_name
        self._dam.registry.register(meta)
    return await self.syscall(tool_name, kwargs)
```

**Risk**: Transient registration pollutes the registry. May need a different approach.
**Status**: Needs more design thought. Defer.

---

### C2. Message adapter hooks on LLMSyscall

```python
llm = LLMSyscall(
    call_fn=my_fn,
    serialize=lambda msg: msg.model_dump(),
    deserialize=lambda d: Message(**d),
)
```

**Status**: Low priority. Message conversion is framework-specific and ~30 lines of adapter code.

---

## Execution Order

1. **A1** — Budget skip (1 file, ~10 lines changed, immediate bug fix)
2. **A2** — ToolMetadata.from_function (1 file, ~20 lines added)
3. **A3** — MemoryCheckpointStore (1 file, ~80 lines added)
4. **B1** — Public properties on Castor (1 file, ~10 lines)
5. **B2** — Default budgets (1 file, ~10 lines)
6. **B4** — Empty schema passthrough (1 file, ~5 lines)
7. Export updates in `__init__.py`
8. Tests for all above
9. Verify existing 249+ tests still pass

**B3** downgraded to docs improvement (existing `cp.pending_tool`/`cp.pending_args` is sufficient).
**C1, C2** deferred — need more design iteration.

## Expected Impact

| Improvement | Integration code eliminated |
|---|---|
| A1 Budget skip | ~10 lines workaround + eliminates a class of bugs |
| A2 from_function() | ~40 lines adapter code |
| A3 MemoryStore | Cleaner test setup across all integrations |
| B1 Public properties | ~20 lines of private-attr access |
| B2 Default budgets | ~10 lines per-call boilerplate |
| B4 Schema skip | ~30 lines LLM wrapper code |
| **Total** | ~110 lines eliminated per integration |
