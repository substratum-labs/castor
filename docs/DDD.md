# Detailed Design Document (DDD): Project Castor

> **Version:** 3.0 (Phase 1 complete — M1 through M4)
> **Status:** Canonical source of truth for data models, APIs, algorithms.
> **See also:** `docs/ADD.md` for architecture overview and data flow diagrams.

---

## 1. System Architecture: Space Isolation

Castor implements a strict **Microkernel Architecture**. The system is divided
into two spaces:

- **User Space (Untrusted):** Contains the LLM inference endpoint, prompt
  templates, and the client UI. Code here cannot directly access the file
  system, network, or other processes.

- **Kernel Space (Trusted):** The Castor Engine. Manages resources,
  capabilities, process scheduling, and actual tool execution.

- **The Bridge (Syscall Interface):** User Space communicates with Kernel Space
  exclusively via `await proxy.syscall(tool_name, arguments)`.

---

## 2. Data Models — Complete API Reference

All models are Pydantic V2 `BaseModel` subclasses. Python 3.11+ built-in types
are used throughout (`dict[str, Any]`, `list[T]`, `X | None`).

### 2.1 Capability (`models/capability.py`)

```python
class Capability(BaseModel):
    resource_type: str       # e.g., "network_read", "disk_delete", "api_usd"
    max_budget: float        # Maximum units available
    current_usage: float = 0.0  # Units consumed so far

    # Derived: remaining = max_budget - current_usage
```

**Invariants:**
- `0 <= current_usage <= max_budget`
- `refund()` clamps at zero: `max(0.0, current_usage - cost)`

### 2.2 SyscallRequest (`models/capability.py`)

```python
class SyscallRequest(BaseModel):
    caller_pid: str             # PID of the calling agent process
    tool_name: str              # Registered tool name
    arguments: dict[str, Any]   # Tool-specific arguments
```

**Note:** `SyscallRequest` is a wire-format model. Inside `SyscallProxy`,
requests are represented as plain dicts `{"tool_name": ..., "arguments": ...}`
for simplicity and serialization efficiency.

### 2.3 SyscallResponse (`models/capability.py`)

```python
class SyscallResponse(BaseModel):
    status: Literal[
        "SUCCESS",
        "VALIDATION_ERROR",
        "HITL_MODIFIED",
        "HITL_REJECTED",
        "SUSPENDED",
        "INSUFFICIENT_CAPABILITY",
    ]
    result_payload: Any | None = None
    feedback_message: str | None = None    # Natural language for LLM self-correction
    human_feedback: str | None = None      # Natural language from HITL reviewer
```

**Status semantics:**

| Status | Meaning | Agent Action |
|---|---|---|
| `SUCCESS` | Tool executed successfully | Use `result_payload` |
| `VALIDATION_ERROR` | Arguments failed Pydantic validation | Read `feedback_message`, fix arguments, retry |
| `HITL_MODIFIED` | Human approved but requested changes | Read `human_feedback`, re-plan with revised args |
| `HITL_REJECTED` | Human rejected the action | Read `human_feedback`, choose alternative approach |
| `SUSPENDED` | Agent execution paused | N/A (agent function unwound) |
| `INSUFFICIENT_CAPABILITY` | Budget exhausted | Read `feedback_message`, reduce scope or abort |

### 2.4 CastorMessage (`models/checkpoint.py`)

```python
class CastorMessage(BaseModel):
    role: str                # "system", "user", "assistant", "tool"
    content: str             # Message text
    pinned: bool = False     # If True, NEVER evicted by Lodge
    token_count: int = 0     # Pre-computed token count (0 = use estimator)
```

**Pinning rules:**
- System prompts must be pinned (`pinned=True`)
- HITL intervention records should be pinned
- Regular conversation messages are unpinned by default

### 2.5 SyscallRecord (`models/checkpoint.py`)

```python
class SyscallRecord(BaseModel):
    request: dict[str, Any]                     # {"tool_name": ..., "arguments": ...}
    response: Any                               # Tool return value, or HITL feedback dict
    was_hitl: bool = False                      # True if this went through HITL review
    child_checkpoint: AgentCheckpoint | None = None  # Nested, for spawn_agent / join_agent
```

**Forward reference:** `child_checkpoint` references `AgentCheckpoint` (defined
below). Resolved via `SyscallRecord.model_rebuild()` at module load time.

**Record types by content:**

| Scenario | `request.tool_name` | `response` | `was_hitl` | `child_checkpoint` |
|---|---|---|---|---|
| Normal tool call | `"read_file"` | Tool return value | `False` | `None` |
| HITL approved | `"delete_file"` | Tool return value | `True` | `None` |
| HITL rejected | `"delete_file"` | `{"status":"HITL_REJECTED","human_feedback":"..."}` | `True` | `None` |
| HITL modified | `"delete_file"` | `{"status":"HITL_MODIFIED","human_feedback":"..."}` | `True` | `None` |
| Validation error | `"bad_tool"` | `{"status":"VALIDATION_ERROR","feedback_message":"..."}` | `False` | `None` |
| Budget exhausted | `"expensive_tool"` | `{"status":"INSUFFICIENT_CAPABILITY","feedback_message":"..."}` | `False` | `None` |
| Sync spawn | `"spawn_agent"` | Child's return value | `False` | Child's `AgentCheckpoint` |
| Async spawn | `"spawn_agent_async"` | Child PID string | `False` | `None` |
| Join | `"join_agent"` | Child's return value | `False` | Child's `AgentCheckpoint` |
| Child HITL (spawn) | `"spawn_agent"` | `None` | `False` | Suspended child's `AgentCheckpoint` |
| Child HITL (join) | `"join_agent"` | `None` | `False` | Suspended child's `AgentCheckpoint` |
| Kernel page-out | `"sys_kernel_page_out"` | Driver confirmation string | `False` | `None` |

### 2.6 AgentCheckpoint (`models/checkpoint.py`)

```python
class AgentCheckpoint(BaseModel):
    pid: str                                     # Unique process identifier
    parent_pid: str | None = None                # Set for child agents
    status: Literal[
        "RUNNING",
        "SUSPENDED_FOR_HITL",
        "PREEMPTED",
        "COMPLETED",
        "FAILED",
    ]
    agent_function_name: str                     # Registry key for replay lookup
    capabilities: dict[str, Capability]          # Budget buckets
    syscall_log: list[SyscallRecord] = []        # The replay journal
    pending_hitl: dict[str, Any] | None = None   # Blocked syscall request
    context_history: list[CastorMessage | dict[str, Any]] = []  # LLM message history
    result: Any | None = None                    # Agent function return value

    # Preemption context (informational, outside replay determinism)
    preemption_reason: str | None = None         # e.g., "HUMAN_ABORT", "TIMEOUT"
    preemption_payload: dict[str, Any] | None = None  # Data from the interrupter
    partial_work: str | None = None              # Mid-thought output hint
```

**Status transitions:**

```
  RUNNING ──────────────────> COMPLETED     (agent_fn returns)
     │
     ├──────────────────────> SUSPENDED_FOR_HITL  (destructive/hitl tool)
     │                              │
     │                              ├──> RUNNING  (approve/reject/modify)
     │                              └──> (stays)  (child resuspends)
     │
     ├──────────────────────> PREEMPTED     (task.cancel())
     │
     └──────────────────────> FAILED        (unhandled exception)
```

**PID format:**
- Root agents: user-chosen string (e.g., `"main-001"`)
- Child agents: `"{parent_pid}::{agent_name}-{N}"` where N is the spawn
  sequence number counting both sync and async spawns

**`pending_hitl` format:**

For direct HITL:
```python
{"tool_name": "delete_file", "arguments": {"path": "/important"}}
```

For child HITL propagation:
```python
{
    "tool_name": "spawn_agent",      # or "join_agent"
    "arguments": {"agent_name": "researcher", ...},
    "child_pid": "main-001::researcher-0"
}
```

### 2.7 SuspendInterrupt (`models/checkpoint.py`)

```python
class SuspendInterrupt(Exception):  # noqa: N818
    """Raised by SyscallProxy to unwind the coroutine stack when HITL is needed."""

    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
```

**Design note:** This is an `Exception`, not an `Error`. The `N818` ruff
suppression is intentional — it's a control flow interrupt, not a programming
error. Naming it `SuspendInterrupt` (not `SuspendError`) communicates this.

---

## 3. Component APIs — Complete Method Signatures

### 3.1 CapabilityManager (`capability/manager.py`)

```python
class CapabilityManager:
    def create_capabilities(
        self, specs: dict[str, float]
    ) -> dict[str, Capability]:
        """Create root capabilities from {resource_type: max_budget} specs.

        Returns a dict mapping resource_type to Capability instances
        with current_usage = 0.0.
        """

    def check(
        self, capabilities: dict[str, Capability],
        resource_type: str, cost: float
    ) -> bool:
        """Check if sufficient budget exists.

        Returns False if resource_type not found or insufficient budget.
        Does NOT modify state.
        """

    def deduct(
        self, capabilities: dict[str, Capability],
        resource_type: str, cost: float
    ) -> None:
        """Deduct cost from a capability budget.

        Raises CapabilityExhaustedError if:
          - resource_type not found (remaining=0.0)
          - remaining < cost
        """

    def refund(
        self, capabilities: dict[str, Capability],
        resource_type: str, cost: float
    ) -> None:
        """Reverse a prior deduction.

        Clamps at zero: current_usage = max(0.0, current_usage - cost)
        No-op if resource_type not found.
        """

    def delegate(
        self, parent_caps: dict[str, Capability],
        requested: dict[str, float]
    ) -> dict[str, Capability]:
        """Partition budget from parent to child.

        ALGORITHM (atomic two-phase):
          Phase 1 (validate): For each resource in requested:
            - Check parent has the resource_type
            - Check parent has sufficient remaining budget
            - If any check fails: raise InsufficientBudgetError (no state modified)
          Phase 2 (commit): For each resource in requested:
            - parent.current_usage += amount
            - Create child Capability(max_budget=amount, current_usage=0)

        Returns the child capabilities dict.
        """

    def reclaim(
        self, parent_caps: dict[str, Capability],
        child_caps: dict[str, Capability]
    ) -> None:
        """Return unused child budget to parent.

        For each child capability:
          unused = max_budget - current_usage
          if unused > 0 and resource in parent: parent.current_usage -= unused
        """
```

**Error classes:**

```python
class CapabilityExhaustedError(Exception):
    resource_type: str
    requested: float
    remaining: float

class InsufficientBudgetError(Exception):
    resource_type: str
    requested: float
    available: float
```

### 3.2 ToolRegistry (`dam/registry.py`)

```python
class ToolMetadata(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    consumes: str                  # Capability resource type
    cost_per_use: float = 1.0
    requires_hitl: bool = False    # Always suspend for review
    destructive: bool = False      # Marks irreversible operations (triggers HITL)
    input_schema: dict[str, Any] = {}  # JSON Schema from function signature
    func: Callable | None = None   # The actual Python function
    is_async: bool = False         # Whether func is a coroutine function


class ToolRegistry:
    _tools: dict[str, ToolMetadata]

    def register(self, metadata: ToolMetadata) -> None
    def get(self, tool_name: str) -> ToolMetadata       # raises ToolNotFoundError
    def has_tool(self, tool_name: str) -> bool
    def list_tools(self) -> list[str]                    # sorted

# Module-level singleton:
default_registry = ToolRegistry()
```

### 3.3 @castor_tool Decorator (`dam/decorator.py`)

```python
def castor_tool(
    consumes: str,
    cost_per_use: float = 1.0,
    requires_hitl: bool = False,
    destructive: bool = False,
    registry: ToolRegistry | None = None,  # defaults to default_registry
) -> Callable:
    """Register a Python function as a Castor tool.

    ALGORITHM:
      1. tool_name = func.__name__
      2. input_schema = _generate_schema(func)
      3. is_async = asyncio.iscoroutinefunction(func)
      4. Create ToolMetadata with all parameters
      5. Register in target_registry
      6. Attach metadata as func._castor_metadata
      7. Return func (unchanged)
    """
```

**Schema generation (`_generate_schema`):**

```python
def _generate_schema(func: Callable) -> dict[str, Any]:
    """
    ALGORITHM:
      1. sig = inspect.signature(func)
      2. annotations = getattr(func, '__annotations__', None) or {}
         (defensive: handles mocks, builtins without __annotations__)
      3. hints = {k: v for annotations items if k != 'return'}
      4. For each parameter in sig.parameters (skip self/cls):
         - If no default: field = (annotation, ...)     # required
         - If has default: field = (annotation, default) # optional
      5. model = pydantic.create_model(f"{func.__name__}_InputModel", **fields)
      6. Return model.model_json_schema()
    """
```

### 3.4 CastorDam Validator (`dam/validator.py`)

```python
class CastorDam:
    _input_models: dict[str, type]  # Lazy-built Pydantic models

    def __init__(self, registry: ToolRegistry) -> None

    def get_tool_meta(self, tool_name: str) -> ToolMetadata
        # Delegates to registry.get()

    def validate(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate arguments against the tool's schema.

        ALGORITHM:
          1. model_cls = _get_or_build_model(tool_name)  [cached]
          2. instance = model_cls(**arguments)
          3. Return instance.model_dump()
             (includes defaults applied by Pydantic)

        Raises pydantic.ValidationError on invalid input.
        """

    async def execute(self, tool_name: str, validated_args: dict[str, Any]) -> Any:
        """Execute a tool with pre-validated arguments.

        ALGORITHM:
          1. meta = registry.get(tool_name)
          2. if meta.func is None: raise RuntimeError
          3. if meta.is_async: return await meta.func(**validated_args)
          4. else: return meta.func(**validated_args)
        """

    def format_validation_error(
        self, tool_name: str, error: ValidationError
    ) -> SyscallResponse:
        """Convert ValidationError to natural language feedback.

        ALGORITHM:
          1. For each error in error.errors():
             - field = " -> ".join(loc parts)
             - msg = error message
             - Append "  - {field}: {msg}"
          2. Build feedback string:
             "Validation failed for tool '{tool_name}':\n{details}\n
              Please fix the arguments and try again."
          3. Return SyscallResponse(status="VALIDATION_ERROR",
                                     feedback_message=feedback)
        """
```

**Input model building (`_build_input_model`):**

```python
def _build_input_model(meta: ToolMetadata) -> type:
    """Build a Pydantic model from a tool function's signature.

    Uses the same defensive annotation access as _generate_schema:
      annotations = getattr(func, '__annotations__', None) or {}

    Returns a dynamically-created Pydantic model class.
    """
```

### 3.5 SyscallProxy (`stream/proxy.py`)

```python
class SyscallProxy:
    # Constructor
    def __init__(
        self,
        checkpoint: AgentCheckpoint,
        dam: CastorDam,
        capability_manager: CapabilityManager,
        lodge: CastorLodge | None = None,
        llm_tool_names: set[str] | None = None,      # default: {"llm_inference"}
        kernel_tool_names: set[str] | None = None,    # default: set()
        agent_registry: AgentRegistry | None = None,
    ) -> None

    @property
    def is_replaying(self) -> bool:
        """True when _replay_index < len(checkpoint.syscall_log)."""

    async def syscall(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Main entry point. See Section 4 for complete algorithm."""
```

**Internal methods (private):**

```python
    async def _handle_spawn(self, request, arguments) -> Any
    async def _handle_spawn_async(self, request, arguments) -> str
    async def _handle_join(self, request, arguments) -> Any
    def _propagate_child_suspension(self, request, child_cp) -> None
    def _append_record(self, record: SyscallRecord) -> None
```

### 3.6 AgentRunner (`stream/runner.py`)

```python
class AgentRunner:
    def __init__(
        self,
        dam: CastorDam,
        capability_manager: CapabilityManager,
        lodge: CastorLodge | None = None,
        agent_registry: AgentRegistry | None = None,
    ) -> None

    async def run(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        checkpoint: AgentCheckpoint,
    ) -> AgentCheckpoint:
        """Direct execution. Returns updated checkpoint.

        ALGORITHM:
          1. Set status = "RUNNING"
          2. Create SyscallProxy with all kernel subsystems
          3. try:
               result = await agent_fn(proxy)
               status = "COMPLETED", store result
             except SuspendInterrupt:
               pass (checkpoint already set by proxy)
             except CancelledError:
               status = "PREEMPTED"
               raise (propagate cancellation)
          4. Return checkpoint
        """

    async def run_as_task(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        checkpoint: AgentCheckpoint,
    ) -> asyncio.Task:
        """Background execution. Returns asyncio.Task for preemption."""

    def preempt(self, reason: str, payload: dict | None = None) -> None:
        """Set preemption context on checkpoint, then cancel the task.

        PRECONDITION: task exists and is not done.
        Sets checkpoint.preemption_reason and preemption_payload.
        """
```

### 3.7 HITLHandler (`stream/hitl.py`)

```python
class HITLHandler:
    # ── Primary operations ──

    async def approve(
        self,
        checkpoint: AgentCheckpoint,
        dam: CastorDam,
        capability_manager: CapabilityManager,
    ) -> None:
        """Execute the blocked syscall.

        ALGORITHM:
          1. Validate: checkpoint.pending_hitl is not None
          2. Extract tool_name and arguments from pending_hitl
          3. Dam.validate(tool_name, arguments)
          4. Cap.deduct(capabilities, consumes, cost)
          5. result = await Dam.execute(tool_name, validated_args)
          6. Append SyscallRecord(request, response=result, was_hitl=True)
          7. Clear pending_hitl, set status = "RUNNING"
        """

    def reject(self, checkpoint: AgentCheckpoint, feedback: str) -> None:
        """Log HITL_REJECTED with human feedback.

        ALGORITHM:
          1. Validate: pending_hitl is not None
          2. Append SyscallRecord(
               request=pending_hitl,
               response={"status":"HITL_REJECTED","human_feedback": feedback},
               was_hitl=True)
          3. Clear pending_hitl, set status = "RUNNING"
        """

    def modify(self, checkpoint: AgentCheckpoint, feedback: str) -> None:
        """Log HITL_MODIFIED with human feedback.

        Same as reject but with status="HITL_MODIFIED".
        On replay, the LLM sees the feedback and issues a revised syscall.
        """

    # ── Child HITL operations ──

    def is_child_hitl(self, checkpoint: AgentCheckpoint) -> bool:
        """True if pending_hitl.tool_name is 'spawn_agent' or 'join_agent'."""

    async def approve_child_hitl(
        self, checkpoint, dam, capability_manager, agent_registry,
        lodge=None
    ) -> None:
        """Approve child's HITL, then resume child.

        ALGORITHM:
          1. Guard: is_child_hitl must be True
          2. child_cp = last syscall record's child_checkpoint
          3. approve(child_cp, dam, cap_mgr)
          4. _resume_child(parent_cp, child_cp, ...)
        """

    async def reject_child_hitl(
        self, checkpoint, feedback, dam, cap_mgr, agent_registry, lodge=None
    ) -> None
        # Same pattern: reject child HITL, then _resume_child

    async def modify_child_hitl(
        self, checkpoint, feedback, dam, cap_mgr, agent_registry, lodge=None
    ) -> None
        # Same pattern: modify child HITL, then _resume_child

    # ── Internal ──

    def _get_child_checkpoint(self, checkpoint) -> AgentCheckpoint:
        """Extract child checkpoint from parent's last syscall record."""

    async def _resume_child(
        self, parent_cp, child_cp, dam, cap_mgr, agent_registry, lodge=None
    ) -> None:
        """Replay child agent after HITL resolution.

        ALGORITHM:
          1. Look up agent_fn from agent_registry using child_cp.agent_function_name
          2. Create fresh SyscallProxy for child (with existing syscall_log)
          3. try:
               result = await agent_fn(child_proxy)
               child_cp.result = result
               child_cp.status = "COMPLETED"
             except SuspendInterrupt:
               Update parent's last record with new child_cp
               Return (parent stays SUSPENDED)
          4. On completion:
             - reclaim(parent_caps, child_caps)
             - Update parent's last record: response = result, child_cp
             - Clear parent's pending_hitl, set RUNNING
        """
```

### 3.8 AgentRegistry (`stream/agent_registry.py`)

```python
AgentFn = Callable[..., Awaitable[Any]]

class AgentRegistry:
    _agents: dict[str, AgentFn]

    def register(self, name: str, fn: AgentFn) -> None
    def get(self, name: str) -> AgentFn           # raises AgentNotFoundError
    def has_agent(self, name: str) -> bool
    def list_agents(self) -> list[str]            # sorted

def castor_agent(
    name: str | None = None,     # defaults to fn.__name__
    *, registry: AgentRegistry
) -> Callable:
    """Decorator to register an async function as a Castor agent."""
```

### 3.9 CheckpointStore (`stream/persistence.py`)

```python
class CheckpointStore:
    def __init__(self, db_url: str = "sqlite:///castor.db") -> None
        # Creates SQLAlchemy engine and "checkpoints" table

    def save(self, checkpoint: AgentCheckpoint) -> None:
        """Upsert: serialize via model_dump_json(), store with UTC timestamp."""

    def load(self, pid: str) -> AgentCheckpoint:
        """Deserialize via model_validate_json(). Raises CheckpointNotFoundError."""

    def delete(self, pid: str) -> None
    def list_pids(self) -> list[str]
```

**Database schema:**

```sql
CREATE TABLE checkpoints (
    pid   TEXT PRIMARY KEY,
    data  TEXT NOT NULL,           -- JSON-serialized AgentCheckpoint
    updated_at DATETIME NOT NULL   -- UTC timestamp
);
```

### 3.10 CastorLodge (`lodge/core.py`)

```python
class CastorLodge:
    def __init__(
        self,
        registry: ToolRegistry,
        driver: SemanticMemoryDriver,
        token_counter: TokenCounter | None = None,  # default: CharCountEstimator
        watermark: int = 8000,                       # token threshold
        consumes: str = "system",                    # capability for page-out
        cost_per_use: float = 0.0,                   # free by default
    ) -> None:
        """Initialize Lodge and register kernel tools.

        SIDE EFFECTS:
          1. Registers "sys_kernel_page_out" tool in registry
             - Kernel-internal, not visible to LLM
             - Closure captures driver.ingest()
             - is_async=True
          2. Registers "search_memory" tool in registry
             - User-facing, LLM can call via proxy.syscall()
             - Closure captures driver.search()
             - is_async=True
        """

    @property
    def kernel_tool_names(self) -> set[str]:
        """Returns {"sys_kernel_page_out"}."""

    def total_tokens(self, checkpoint: AgentCheckpoint) -> int:
        """Sum token counts of CastorMessage entries in context_history.

        ALGORITHM:
          For each entry in context_history:
            if isinstance(entry, CastorMessage):
              if entry.token_count > 0: total += entry.token_count
              else: total += self._counter.count(entry.content)
            # Plain dicts are ignored (no eviction participation)
          Return total
        """

    def _select_victims(self, checkpoint: AgentCheckpoint) -> list[CastorMessage]:
        """FIFO eviction: oldest unpinned messages first.

        ALGORITHM:
          running_total = total_tokens(checkpoint)
          victims = []
          for entry in context_history:  # oldest first
            if running_total <= watermark: break
            if isinstance(entry, CastorMessage) and not entry.pinned:
              tokens = entry.token_count or counter.count(entry.content)
              victims.append(entry)
              running_total -= tokens
          return victims
        """

    async def check_and_evict(
        self, proxy: SyscallProxy, checkpoint: AgentCheckpoint
    ) -> None:
        """Check token usage and evict if over watermark.

        PRECONDITION: Called only during live execution (not replay).
        The SyscallProxy.syscall() method gates this with:
          if lodge and not is_replaying and tool_name in llm_tool_names

        ALGORITHM:
          1. if total_tokens(cp) <= watermark: return
          2. victims = _select_victims(cp)
          3. if not victims: return
          4. payload = [{**v.model_dump(), "_pid": cp.pid} for v in victims]
          5. await proxy.syscall("sys_kernel_page_out",
                                 {"messages_json": json.dumps(payload)})
          6. victim_set = set(id(v) for v in victims)
          7. cp.context_history = [e for e if id(e) not in victim_set]

        CRITICAL: Step 6 uses object identity (id()) not equality.
        Messages must NOT be copied before eviction, or the id()
        matching will fail and messages won't be removed.
        """
```

### 3.11 SemanticMemoryDriver ABC (`lodge/driver.py`)

```python
class SemanticMemoryDriver(ABC):
    @abstractmethod
    async def ingest(self, messages: list[dict[str, Any]], pid: str) -> str:
        """Store evicted messages in cold storage.
        Returns a confirmation string (logged in syscall_log)."""

    @abstractmethod
    async def search(self, query: str, pid: str) -> str:
        """Search cold storage and return relevant content as text."""
```

### 3.12 TokenCounter Protocol (`lodge/token_counter.py`)

```python
@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...

class CharCountEstimator:
    """Default: max(1, len(text) // 4). No tiktoken dependency."""
    def count(self, text: str) -> int:
        return max(1, len(text) // 4)
```

### 3.13 InMemoryDriver (`lodge/drivers/mock_driver.py`)

```python
class InMemoryDriver(SemanticMemoryDriver):
    """Dict-based storage with substring search. Testing only."""
    _store: dict[str, list[str]]   # pid -> list of JSON strings

    async def ingest(self, messages, pid) -> str:
        # Appends json.dumps(msg) for each message
        # Returns "Ingested N messages for pid=..."

    async def search(self, query, pid) -> str:
        # Returns newline-joined entries where query.lower() in entry.lower()
        # Returns "No matching memories found." if empty
```

### 3.14 LLMSyscall (`llm/wrapper.py`)

```python
class LLMSyscall:
    def __init__(
        self,
        registry: ToolRegistry,
        call_fn: Callable[..., Any],      # async def(model, prompt) -> str
        consumes: str = "api_usd",
        cost_per_use: float = 1.0,
        tool_name: str = "llm_inference",
    ) -> None:
        """Register the LLM client as a Castor tool.

        ALGORITHM:
          1. Try _generate_schema(call_fn) for input schema
          2. Fallback to {} for callables without annotations
          3. Create ToolMetadata(is_async=True, ...)
          4. Register in registry
        """

    async def infer(self, proxy: SyscallProxy, **kwargs: Any) -> Any:
        """Issue LLM inference through the proxy.

        Equivalent to: await proxy.syscall(self._tool_name, kwargs)
        During replay: cached response returned (LLM NOT called).
        During live: call_fn executed and result logged.
        """
```

---

## 4. Core Algorithms — Pseudocode

### 4.1 SyscallProxy.syscall() — Complete Flow

```
FUNCTION syscall(tool_name, arguments):
    request = {"tool_name": tool_name, "arguments": arguments}

    # ── Step 1: Lodge eviction hook ──
    IF lodge is not None
       AND NOT is_replaying
       AND tool_name IN llm_tool_names:
        CALL lodge.check_and_evict(self, checkpoint)

    # ── Step 2: Skip kernel tool records during replay ──
    WHILE _replay_index < LEN(syscall_log):
        record = syscall_log[_replay_index]
        IF record.request.tool_name NOT IN kernel_tool_names:
            BREAK
        _replay_index += 1

    # ── Step 3: Replay cache hit ──
    IF _replay_index < LEN(syscall_log):
        record = syscall_log[_replay_index]
        IF record.request != request:
            RAISE ReplayDivergenceError(_replay_index, record.request, request)
        _replay_index += 1
        RETURN record.response

    # ── Step 4: Spawn/Join intercept ──
    IF tool_name == "spawn_agent":
        RETURN AWAIT _handle_spawn(request, arguments)
    IF tool_name == "spawn_agent_async":
        RETURN AWAIT _handle_spawn_async(request, arguments)
    IF tool_name == "join_agent":
        RETURN AWAIT _handle_join(request, arguments)

    # ── Step 5: Dam validation ──
    TRY:
        validated = dam.validate(tool_name, arguments)
    EXCEPT ValidationError AS e:
        response = dam.format_validation_error(tool_name, e)
        _append_record(SyscallRecord(request, response.model_dump()))
        RETURN response.model_dump()

    tool_meta = dam.get_tool_meta(tool_name)

    # ── Step 6: HITL gate ──
    IF tool_meta.requires_hitl OR tool_meta.destructive:
        checkpoint.pending_hitl = request
        checkpoint.status = "SUSPENDED_FOR_HITL"
        RAISE SuspendInterrupt(checkpoint)

    # ── Step 7: Capability deduction ──
    TRY:
        cap_mgr.deduct(checkpoint.capabilities,
                        tool_meta.consumes, tool_meta.cost_per_use)
    EXCEPT CapabilityExhaustedError AS e:
        response = SyscallResponse(status="INSUFFICIENT_CAPABILITY",
                                    feedback_message=STR(e))
        _append_record(SyscallRecord(request, response.model_dump()))
        RETURN response.model_dump()

    # ── Step 8: Execute with refund safety ──
    TRY:
        result = AWAIT dam.execute(tool_name, validated)
    EXCEPT BaseException:
        cap_mgr.refund(checkpoint.capabilities,
                        tool_meta.consumes, tool_meta.cost_per_use)
        RAISE

    # ── Step 9: Log and return ──
    _append_record(SyscallRecord(request, result))
    RETURN result
```

### 4.2 Synchronous Spawn (`_handle_spawn`)

```
FUNCTION _handle_spawn(request, arguments):
    GUARD: agent_registry is not None

    agent_name = arguments["agent_name"]
    requested_caps = arguments.get("capabilities", {})

    # 1. Look up agent
    agent_fn = agent_registry.get(agent_name)

    # 2. Delegate capabilities (atomic)
    child_caps = cap_mgr.delegate(checkpoint.capabilities, requested_caps)

    # 3. Deterministic PID
    spawn_count = COUNT records WHERE tool_name IN {spawn_agent, spawn_agent_async}
    child_pid = f"{checkpoint.pid}::{agent_name}-{spawn_count}"

    # 4. Create child checkpoint
    child_cp = AgentCheckpoint(
        pid=child_pid, parent_pid=checkpoint.pid,
        status="RUNNING", agent_function_name=agent_name,
        capabilities=child_caps)

    # 5. Create child proxy (shares dam, cap_mgr, lodge, registry)
    child_proxy = SyscallProxy(child_cp, dam, cap_mgr, lodge, ..., agent_registry)

    # 6. Execute child
    TRY:
        child_result = AWAIT agent_fn(child_proxy)
        child_cp.result = child_result
        child_cp.status = "COMPLETED"
    EXCEPT SuspendInterrupt:
        _propagate_child_suspension(request, child_cp)
        RAISE SuspendInterrupt(checkpoint)
    EXCEPT BaseException:
        cap_mgr.reclaim(checkpoint.capabilities, child_cp.capabilities)
        RAISE

    # 7. Reclaim unused budget
    cap_mgr.reclaim(checkpoint.capabilities, child_cp.capabilities)

    # 8. Log and return
    _append_record(SyscallRecord(request, child_result, child_checkpoint=child_cp))
    RETURN child_result
```

### 4.3 Asynchronous Spawn (`_handle_spawn_async`)

```
FUNCTION _handle_spawn_async(request, arguments):
    GUARD: agent_registry is not None

    agent_name = arguments["agent_name"]
    requested_caps = arguments.get("capabilities", {})
    agent_fn = agent_registry.get(agent_name)

    # Delegate capabilities
    child_caps = cap_mgr.delegate(checkpoint.capabilities, requested_caps)

    TRY:
        # PID generation (same counter as sync)
        spawn_count = COUNT records WHERE tool_name IN {spawn_agent, spawn_agent_async}
        child_pid = f"{checkpoint.pid}::{agent_name}-{spawn_count}"

        # Create child checkpoint
        child_cp = AgentCheckpoint(pid=child_pid, ...)

        # Create child proxy
        child_proxy = SyscallProxy(child_cp, ...)

        # Define wrapper coroutine
        ASYNC FUNCTION _run_child():
            TRY:
                result = AWAIT agent_fn(child_proxy)
                child_cp.result = result
                child_cp.status = "COMPLETED"
                RETURN result
            EXCEPT SuspendInterrupt:
                RETURN None    # Don't re-raise; parent detects at join
            EXCEPT BaseException:
                child_cp.status = "FAILED"
                RAISE

        # Launch as background task
        task = asyncio.create_task(_run_child())
        _async_tasks[child_pid] = task
        _async_checkpoints[child_pid] = child_cp

    EXCEPT BaseException:
        # Reclaim budget if anything fails after delegation
        cap_mgr.reclaim(checkpoint.capabilities, child_caps)
        RAISE

    # Log and return handle
    _append_record(SyscallRecord(request, response=child_pid))
    RETURN child_pid
```

### 4.4 Join (`_handle_join`)

```
FUNCTION _handle_join(request, arguments):
    handle = arguments["handle"]
    GUARD: handle IN _async_tasks

    task = _async_tasks[handle]
    child_cp = _async_checkpoints[handle]

    # Await child
    TRY:
        AWAIT task
    EXCEPT BaseException:
        cap_mgr.reclaim(checkpoint.capabilities, child_cp.capabilities)
        DELETE _async_tasks[handle], _async_checkpoints[handle]
        RAISE

    # Clean up tracking
    DELETE _async_tasks[handle], _async_checkpoints[handle]

    # Check child status
    IF child_cp.status == "SUSPENDED_FOR_HITL":
        _propagate_child_suspension(request, child_cp)
        RAISE SuspendInterrupt(checkpoint)

    # Child completed
    cap_mgr.reclaim(checkpoint.capabilities, child_cp.capabilities)
    _append_record(SyscallRecord(request, child_cp.result, child_checkpoint=child_cp))
    RETURN child_cp.result
```

### 4.5 Child HITL Propagation

```
FUNCTION _propagate_child_suspension(request, child_cp):
    # 1. Log record with suspended child
    _append_record(SyscallRecord(
        request=request,
        response=None,
        child_checkpoint=child_cp))

    # 2. Set parent's pending HITL
    checkpoint.pending_hitl = {
        "tool_name": request["tool_name"],     # "spawn_agent" or "join_agent"
        "arguments": request["arguments"],
        "child_pid": child_cp.pid
    }
    checkpoint.status = "SUSPENDED_FOR_HITL"
    # Caller raises SuspendInterrupt(checkpoint)
```

### 4.6 Lodge FIFO Eviction

```
FUNCTION check_and_evict(proxy, checkpoint):
    # Called ONLY during live execution (not replay)
    IF total_tokens(checkpoint) <= watermark:
        RETURN

    victims = _select_victims(checkpoint)
    IF NOT victims:
        RETURN

    # Serialize with PID tag
    payload = [{**v.model_dump(), "_pid": checkpoint.pid} FOR v IN victims]

    # Route through proxy for replay safety
    AWAIT proxy.syscall("sys_kernel_page_out",
                         {"messages_json": json.dumps(payload)})

    # Remove from context_history using object identity
    victim_ids = SET(id(v) FOR v IN victims)
    checkpoint.context_history = [
        entry FOR entry IN checkpoint.context_history
        IF id(entry) NOT IN victim_ids
    ]

FUNCTION _select_victims(checkpoint):
    running_total = total_tokens(checkpoint)
    victims = []
    FOR entry IN checkpoint.context_history:  # oldest first (FIFO)
        IF running_total <= watermark:
            BREAK
        IF entry IS CastorMessage AND NOT entry.pinned:
            tokens = entry.token_count IF > 0 ELSE counter.count(entry.content)
            victims.append(entry)
            running_total -= tokens
    RETURN victims
```

### 4.7 Delegate/Reclaim (Atomic Two-Phase)

```
FUNCTION delegate(parent_caps, requested):
    # Phase 1: Validate ALL (no state change)
    FOR (resource, amount) IN requested:
        cap = parent_caps.get(resource)
        IF cap IS None:
            RAISE InsufficientBudgetError(resource, amount, 0.0)
        available = cap.max_budget - cap.current_usage
        IF available < amount:
            RAISE InsufficientBudgetError(resource, amount, available)

    # Phase 2: Commit ALL
    child_caps = {}
    FOR (resource, amount) IN requested:
        parent_caps[resource].current_usage += amount
        child_caps[resource] = Capability(resource_type=resource,
                                           max_budget=amount)
    RETURN child_caps

FUNCTION reclaim(parent_caps, child_caps):
    FOR (resource, child_cap) IN child_caps:
        unused = child_cap.max_budget - child_cap.current_usage
        IF unused > 0 AND resource IN parent_caps:
            parent_caps[resource].current_usage -= unused
```

---

## 5. HITL Feedback Loop — Detailed Protocol

### 5.1 Approve (Execute As-Is)

1. Load `AgentCheckpoint` from SQLite
2. Extract `pending_hitl` → `{tool_name, arguments}`
3. `dam.validate(tool_name, arguments)` → validated args
4. `cap_mgr.deduct(capabilities, consumes, cost)`
5. `result = await dam.execute(tool_name, validated_args)`
6. Append `SyscallRecord(request=pending_hitl, response=result, was_hitl=True)`
7. Clear `pending_hitl`, set status = `"RUNNING"`
8. Replay agent function from top — all syscalls served from cache until new

### 5.2 Reject

1. Load checkpoint
2. Append `SyscallRecord(request=pending_hitl,
     response={"status":"HITL_REJECTED","human_feedback": feedback},
     was_hitl=True)`
3. Clear `pending_hitl`, set status = `"RUNNING"`
4. Replay — LLM sees rejection feedback, re-plans

### 5.3 Approve with Modification

1. Load checkpoint
2. Append `SyscallRecord(request=pending_hitl,
     response={"status":"HITL_MODIFIED","human_feedback": feedback},
     was_hitl=True)`
3. Clear `pending_hitl`, set status = `"RUNNING"`
4. Replay — LLM sees modification feedback, issues revised syscall
5. Revised syscall is a NEW entry in the log (index N+1)

**Why not mutate `pending_hitl`:** Mutating the request would cause replay
divergence — on replay, the agent function emits the original request, but the
log would contain a modified one, failing the replay assertion. Instead, the
modification is pure feedback: the LLM translates natural language into revised
arguments.

### 5.4 Child HITL Resolution

```
Parent suspended (pending_hitl has child_pid)
  │
  ├─ Human decides on child's blocked tool
  │
  ├─ HITLHandler.approve_child_hitl / reject_child / modify_child
  │     │
  │     ├─ Resolve child's pending_hitl (approve/reject/modify)
  │     │
  │     └─ _resume_child():
  │           Create fresh SyscallProxy for child (with existing syscall_log)
  │           Replay child from top
  │           │
  │           ├─ Child suspends again → update parent's last record
  │           │   Parent stays SUSPENDED_FOR_HITL
  │           │
  │           └─ Child completes → reclaim budget
  │               Update parent's last record with result
  │               Clear parent's pending_hitl, set RUNNING
  │               Parent can now be replayed
  │
  └─ Parent replayed from top → spawn/join cached → continues live
```

---

## 6. CLI Design (`cli.py`)

### 6.1 Command Reference

```
castor [--db <path>] <command>

Commands:
  list                         List all checkpoints with status markers
  show <pid>                   Show checkpoint details
  reject <pid> --feedback "…"  Reject pending HITL
  modify <pid> --feedback "…"  Modify pending HITL with feedback

Options:
  --db <path>    SQLite database path (default: castor.db)
```

### 6.2 Status Markers

| Marker | Status | Meaning |
|---|---|---|
| `HITL` | `SUSPENDED_FOR_HITL` | Waiting for human decision |
| `DONE` | `COMPLETED` | Agent finished successfully |
| `RUN ` | `RUNNING` | Agent executing (or ready to resume) |
| `PREM` | `PREEMPTED` | Agent was preempted |
| `FAIL` | `FAILED` | Agent encountered unhandled error |

### 6.3 Safety Guards

- **No `approve` command:** Approve requires Dam + CapabilityManager runtime
  to validate and execute the blocked tool. CLI only has SQLite access.
- **Child HITL guard:** `reject` and `modify` check `is_child_hitl()`.
  If the pending HITL is from `spawn_agent` or `join_agent`, the CLI refuses
  and directs the user to the host application's resume loop (which has the
  full kernel runtime).

---

## 7. Full Kernel Lifecycle Trace

A complete trace: "Clean up my inbox and summarize the retention policy."

```
Time   Component         Event
─────  ────────────────  ──────────────────────────────────────────────────
t0     User Space        User submits task
t1     Castor Stream     Creates AgentCheckpoint(pid="main-001", syscall_log=[])
                         Runs main_agent(proxy)

t2     SyscallProxy      proxy.syscall("list_emails", {older_than: 30})
       ├─ Replay Check   replay_index=0, log empty → NEW syscall
       ├─ Castor Dam     Validates {older_than: int} → OK
       ├─ Cap Manager    cost=1 network_read, budget=100 → OK → Fast Path
       └─ Execute        list_emails() → 847 emails
       ▸ syscall_log = [{list_emails → 847 emails}]

t3     SyscallProxy      proxy.syscall("spawn_agent", {agent_name: "researcher"})
       ├─ Replay Check   replay_index=1, log has 1 → NEW syscall
       ├─ Spawn Intercept  → _handle_spawn()
       ├─ Cap Manager    Delegates 10 network_read (100→90)
       └─ Castor Stream  Creates child checkpoint, runs researcher_agent()

t4       Child Proxy     child_proxy.syscall("web_search", {q: "email retention"})
         ├─ Dam          OK
         ├─ Cap Manager  child: 10→9
         └─ Execute      web_search() → "90-day policy"
         ▸ child syscall_log = [{web_search → "90-day policy"}]

t5       Child           researcher_agent returns {"policy": "90 days"}
         Castor Stream   Child COMPLETED. Reclaim 9 unused → parent: 90→99
       ▸ parent syscall_log = [{list_emails→...}, {spawn_agent→{policy:"90 days"}}]

t6     SyscallProxy      proxy.syscall("delete_emails", {ids: [847 ids]})
       ├─ Replay Check   replay_index=2, log has 2 → NEW syscall
       ├─ Castor Dam     Validates {ids: List[str]} → OK
       ├─ HITL Gate      destructive=True → SLOW PATH
       └─ SyscallProxy   Sets pending_hitl, raises SuspendInterrupt
       ▸ checkpoint = {
           syscall_log: [list_emails, spawn_agent],
           pending_hitl: {tool: "delete_emails", args: {ids: [847]}},
           status: "SUSPENDED_FOR_HITL"
         }

t7     Castor Stream     Catches SuspendInterrupt
                         checkpoint.model_dump_json() → SQLite

       ─── 2 hours pass. Human reviews. ───

t8     Human             "Only delete emails older than 7 days"
       HITLHandler       modify(checkpoint, feedback)
                         Appends {delete_emails → HITL_MODIFIED} to log
                         Clears pending_hitl
       ▸ syscall_log = [list_emails, spawn_agent, delete_emails(MODIFIED)]

t9     Castor Stream     REPLAYS main_agent(proxy) from the top
                         proxy.syscall("list_emails")    → index 0, CACHED
                         proxy.syscall("spawn_agent")    → index 1, CACHED
                         proxy.syscall("delete_emails")  → index 2, CACHED → HITL_MODIFIED
                         Agent feeds human feedback to LLM. LLM re-plans.

t10    SyscallProxy      proxy.syscall("delete_emails", {ids: [712 ids]})  ← revised
       ├─ Replay Check   replay_index=3, log has 3 → NEW syscall
       ├─ Castor Dam     Validates → OK
       ├─ HITL Gate      (may fast-path if human pre-approved intent)
       └─ Execute        delete_emails(712 ids) → OK
       ▸ syscall_log = [..., delete_emails(MODIFIED), delete_emails(712, OK)]

t11    SyscallProxy      proxy.syscall("send_summary", {body: "Deleted 712 emails"})
       ├─ NEW → Fast Path → execute
       └─ Execute        send_summary() → OK

t12    Castor Stream     main_agent returns. status = COMPLETED.
                         Final syscall_log: 5 entries. Full audit trail.
```

---

## 8. Error Catalog

| Error Class | Module | Raised By | Meaning |
|---|---|---|---|
| `CapabilityExhaustedError` | `capability/manager.py` | `deduct()` | Budget insufficient for the requested cost |
| `InsufficientBudgetError` | `capability/manager.py` | `delegate()` | Parent cannot cover child delegation |
| `ToolNotFoundError` | `dam/registry.py` | `get()` | Tool name not in registry |
| `ReplayDivergenceError` | `stream/proxy.py` | `syscall()` | Replay request doesn't match recorded log |
| `AgentNotFoundError` | `stream/agent_registry.py` | `get()` | Agent name not in registry |
| `CheckpointNotFoundError` | `stream/persistence.py` | `load()` | PID not found in SQLite |
| `SuspendInterrupt` | `models/checkpoint.py` | `syscall()`, `_handle_spawn()` | HITL suspension — unwinds stack |
| `ValidationError` | Pydantic | `dam.validate()` | Arguments fail schema validation |
