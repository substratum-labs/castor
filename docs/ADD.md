# Architecture Design Document (ADD): Project Castor

> **Version:** 3.0 (Phase 1 complete — M1 through M4)
> **Status:** Reflects fully implemented state — 170 tests passing, 0 lint errors.
> **Canonical data model reference:** `docs/DDD.md`

---

## 1. High-Level Architecture Overview

Castor is a microkernel for LLM agents. It interposes a deterministic execution
engine between the non-deterministic LLM and the outside world. Every side
effect an agent performs — tool calls, LLM inference, file I/O — must pass
through a single gateway (the syscall interface), where the kernel validates
arguments, enforces capability budgets, and records the result for checkpoint
and replay.

### 1.1 Architecture Layers

```
+===========================================================================+
|                          USER SPACE  (untrusted)                          |
|                                                                           |
|   +------------------+     +------------------+     +------------------+  |
|   |    LLM Client    |     |   Agent Function |     |    Client UI     |  |
|   | (OpenAI, Claude, |     |  async def agent |     | (CLI, Web, Bot)  |  |
|   |   local model)   |     |    (proxy) ->Any |     |                  |  |
|   +--------+---------+     +--------+---------+     +--------+---------+  |
|            |                        |                        |            |
+============|========================|========================|============+
             |     proxy.syscall()    |                        |
             v                        v                        v
+===========================================================================+
|                    SYSCALL INTERFACE  (trust boundary)                     |
|                                                                           |
|                         SyscallProxy                                      |
|              replay | validate | gate | execute | log                     |
+===========================================================================+
             |           |           |           |           |
             v           v           v           v           v
+===========================================================================+
|                         KERNEL SPACE  (trusted)                           |
|                                                                           |
|  +-----------+  +-----------+  +-----------+  +-----------+  +--------+  |
|  | Castor    |  | Castor    |  | Capability|  | Castor    |  |  LLM   |  |
|  | Dam       |  | Stream    |  | Manager   |  | Lodge     |  | Syscall|  |
|  | (tool     |  | (sched +  |  | (budget   |  | (context  |  |(replay |  |
|  |  registry |  |  replay)  |  |  caps)    |  |  MMU)     |  | safe)  |  |
|  |  + valid) |  |           |  |           |  |           |  |        |  |
|  +-----+-----+  +-----+-----+  +-----+-----+  +-----+-----+  +---+----+  |
|        |              |              |              |              |       |
|        +--------------+--------------+--------------+--------------+       |
|                                     |                                     |
|                              SQLite (persistence)                         |
+===========================================================================+
```

**User Space** contains the LLM provider, the agent function, and the UI. None
of these components may execute side effects directly. The agent function
receives a `SyscallProxy` and must route all operations through
`await proxy.syscall(tool_name, args)`.

**Kernel Space** contains the four subsystems (Dam, Stream, Lodge, Capability
Manager) plus the LLM syscall wrapper. The kernel is fully deterministic: given
the same syscall log, replaying an agent function from the top produces the
same execution trace.

**The Syscall Interface** is `SyscallProxy`, the single bridge. It decides
whether to serve a cached response (replay) or execute live, enforcing the
complete validation/capability/HITL pipeline on every live call.

### 1.2 Design Principles

1. **All side effects through syscall.** No direct network, LLM, or I/O calls
   from agent functions.
2. **Checkpoint/Replay, not coroutine serialization.** Python coroutines cannot
   be pickled. Agent state is the `syscall_log` replay journal. Suspend = raise
   `SuspendInterrupt` to unwind the stack. Resume = replay from the top, serve
   cached responses.
3. **Capability-based security.** Every tool declares a resource type and cost.
   Budgets are finite, delegatable, and reclaimable.
4. **Preemptive HITL.** Destructive or high-stakes operations suspend
   automatically. Humans approve, reject, or modify before execution.
5. **HAL-based extensibility.** Lodge uses a `SemanticMemoryDriver` ABC so
   concrete vector stores / embedding models are never imported by kernel code.

### 1.3 System Context Diagram

```
 ┌─────────────────────┐
 │     Developers /    │
 │     End Users       │
 │                     │
 └──────────┬──────────┘
            │ castor CLI / SDK API
            v
 ┌──────────────────────────────────────────────────────┐
 │                   Castor Kernel                      │
 │                                                      │
 │  Agent Function ──syscall()──> SyscallProxy          │
 │       │                           │                  │
 │       │     ┌─────────┬───────────┼──────────┐       │
 │       │     │         │           │          │       │
 │       │   Dam    Capability    Stream     Lodge      │
 │       │ (valid)   (budget)    (replay)   (MMU)       │
 │       │     │         │           │          │       │
 │       │     └─────────┴───────────┼──────────┘       │
 │       │                           │                  │
 │       └────── LLMSyscall ─────────┘                  │
 │                                                      │
 └──────────────┬──────────────┬────────────────────────┘
                │              │
      ┌─────────┴──────┐   ┌──┴──────────────┐
      │  SQLite         │   │  External LLM   │
      │  (checkpoints)  │   │  Provider       │
      └────────────────┘   └─────────────────┘
```

---

## 2. Module Layout

Source tree under `src/castor/`:

```
src/castor/
  __init__.py               # Public API re-exports (v0.1.0, 20 symbols)
  cli.py                    # CLI entry point (list, show, reject, modify)
  models/
    __init__.py             # Re-exports: Capability, SyscallRequest, SyscallResponse,
    |                       #   AgentCheckpoint, SuspendInterrupt, SyscallRecord
    capability.py           # Capability, SyscallRequest, SyscallResponse
    checkpoint.py           # CastorMessage, SyscallRecord, AgentCheckpoint,
                            #   SuspendInterrupt
  capability/
    __init__.py             # Re-exports: CapabilityManager, errors
    manager.py              # CapabilityManager, CapabilityExhaustedError,
                            #   InsufficientBudgetError
  dam/
    __init__.py             # Re-exports: CastorDam, ToolRegistry, ToolMetadata,
    |                       #   castor_tool, default_registry, ToolNotFoundError
    registry.py             # ToolRegistry, ToolMetadata, ToolNotFoundError,
    |                       #   default_registry
    decorator.py            # @castor_tool, _generate_schema
    validator.py            # CastorDam, _build_input_model
  stream/
    __init__.py             # Re-exports: SyscallProxy, AgentRunner, HITLHandler,
    |                       #   AgentRegistry, castor_agent, CheckpointStore, errors
    proxy.py                # SyscallProxy, ReplayDivergenceError
    runner.py               # AgentRunner (run, run_as_task, preempt)
    hitl.py                 # HITLHandler (approve, reject, modify, child variants)
    agent_registry.py       # AgentRegistry, @castor_agent, AgentNotFoundError
    persistence.py          # CheckpointStore (SQLAlchemy + SQLite),
                            #   CheckpointNotFoundError
  lodge/
    __init__.py             # Re-exports: CastorLodge, SemanticMemoryDriver,
    |                       #   TokenCounter, CharCountEstimator
    core.py                 # CastorLodge (check_and_evict, _select_victims,
    |                       #   total_tokens, kernel_tool_names)
    driver.py               # SemanticMemoryDriver ABC (ingest, search)
    token_counter.py        # TokenCounter protocol, CharCountEstimator
    drivers/
      __init__.py
      mock_driver.py        # InMemoryDriver (dict + substring search)
  llm/
    __init__.py             # Re-exports: LLMSyscall
    wrapper.py              # LLMSyscall (registers tool, infer via proxy)
```

---

## 3. Detailed Component Architecture

### 3.1 Data Models (`models/`)

All data models are Pydantic V2 `BaseModel` subclasses using Python 3.11+
built-in types (`dict[str, Any]`, not `Dict[str, Any]`). Status fields use
`Literal` types, not Python enums.

#### 3.1.1 Capability & Syscall Types (`capability.py`)

```
┌──────────────────────┐   ┌──────────────────────────────┐
│     Capability       │   │       SyscallRequest         │
├──────────────────────┤   ├──────────────────────────────┤
│ resource_type: str   │   │ caller_pid: str              │
│ max_budget: float    │   │ tool_name: str               │
│ current_usage: float │   │ arguments: dict[str, Any]    │
│   (default 0.0)      │   └──────────────────────────────┘
└──────────────────────┘
                           ┌──────────────────────────────┐
                           │       SyscallResponse        │
                           ├──────────────────────────────┤
                           │ status: Literal[             │
                           │   "SUCCESS",                 │
                           │   "VALIDATION_ERROR",        │
                           │   "HITL_MODIFIED",           │
                           │   "HITL_REJECTED",           │
                           │   "SUSPENDED",               │
                           │   "INSUFFICIENT_CAPABILITY"] │
                           │ result_payload: Any | None   │
                           │ feedback_message: str | None │
                           │ human_feedback: str | None   │
                           └──────────────────────────────┘
```

#### 3.1.2 Checkpoint & Replay Types (`checkpoint.py`)

```
┌──────────────────────────────────────────────────────────────┐
│                      AgentCheckpoint                        │
├──────────────────────────────────────────────────────────────┤
│ pid: str                                                    │
│ parent_pid: str | None                                      │
│ status: Literal["RUNNING","SUSPENDED_FOR_HITL","PREEMPTED", │
│                  "COMPLETED","FAILED"]                       │
│ agent_function_name: str                                    │
│ capabilities: dict[str, Capability]                         │
│ syscall_log: list[SyscallRecord]                            │
│ pending_hitl: dict[str, Any] | None                         │
│ context_history: list[CastorMessage | dict[str, Any]]       │
│ result: Any | None                                          │
│ --- preemption context (informational only) ---             │
│ preemption_reason: str | None                               │
│ preemption_payload: dict[str, Any] | None                   │
│ partial_work: str | None                                    │
└──────────────────────────────────────────────────────────────┘
         │ contains
         v
┌─────────────────────────────┐     ┌─────────────────────────┐
│       SyscallRecord         │     │      CastorMessage      │
├─────────────────────────────┤     ├─────────────────────────┤
│ request: dict[str, Any]     │     │ role: str               │
│ response: Any               │     │ content: str            │
│ was_hitl: bool              │     │ pinned: bool (=False)   │
│ child_checkpoint:           │     │ token_count: int (=0)   │
│   AgentCheckpoint | None    │     └─────────────────────────┘
└─────────────────────────────┘

┌─────────────────────────────┐
│      SuspendInterrupt       │
├─────────────────────────────┤
│ Exception (not Error)       │
│ checkpoint: AgentCheckpoint │
│ # noqa: N818                │
└─────────────────────────────┘
```

Forward reference `SyscallRecord.child_checkpoint -> AgentCheckpoint` is
resolved via `SyscallRecord.model_rebuild()` at module load time.

### 3.2 Capability Manager (`capability/manager.py`)

Implements a token-bucket quota system for all kernel resources.

```
┌──────────────────────────────────────────────────────────┐
│                   CapabilityManager                      │
├──────────────────────────────────────────────────────────┤
│ create_capabilities(specs) -> dict[str, Capability]      │
│ check(caps, resource, cost) -> bool                      │
│ deduct(caps, resource, cost) -> None                     │
│ refund(caps, resource, cost) -> None                     │
│ delegate(parent_caps, requested) -> dict[str, Capability]│
│ reclaim(parent_caps, child_caps) -> None                 │
└──────────────────────────────────────────────────────────┘
```

**Budget lifecycle:**

```
create ───> deduct ───> [refund on failure]
   │
   └──> delegate (parent -= amount, child = amount)
            │
            ├──> child deduct/refund
            │
            └──> reclaim (parent += child.unused)
```

**Atomicity guarantee:** `delegate()` validates all requested resource types
before modifying any capability. If any resource type is missing or has
insufficient budget, `InsufficientBudgetError` is raised and no state changes.

**Error types:**

- `CapabilityExhaustedError(resource_type, requested, remaining)` -- raised by
  `deduct()` when budget is insufficient.
- `InsufficientBudgetError(resource_type, requested, available)` -- raised by
  `delegate()` when parent cannot cover the delegation request.

### 3.3 Castor Dam (`dam/`)

The tool registry and strongly-typed validation boundary.

```
┌───────────────────────────────────────────────────────────────────────┐
│                           Castor Dam                                 │
│                                                                       │
│  ┌──────────────────┐   ┌──────────────────────────────────────────┐ │
│  │   ToolRegistry    │   │             CastorDam                    │ │
│  ├──────────────────┤   ├──────────────────────────────────────────┤ │
│  │ register(meta)   │   │ get_tool_meta(name) -> ToolMetadata      │ │
│  │ get(name) -> meta│   │ validate(name, args) -> validated dict   │ │
│  │ has_tool(name)   │   │ execute(name, args) -> result            │ │
│  │ list_tools()     │   │ format_validation_error(name, err)      │ │
│  └──────────────────┘   │   -> SyscallResponse                     │ │
│                          └──────────────────────────────────────────┘ │
│  ┌──────────────────┐   ┌──────────────────────────────────────────┐ │
│  │  @castor_tool     │   │          ToolMetadata                    │ │
│  ├──────────────────┤   ├──────────────────────────────────────────┤ │
│  │ consumes: str    │   │ tool_name, consumes, cost_per_use       │ │
│  │ cost_per_use     │   │ requires_hitl, destructive              │ │
│  │ requires_hitl    │   │ input_schema: dict (JSON Schema)        │ │
│  │ destructive      │   │ func: Callable | None                   │ │
│  │ registry: opt    │   │ is_async: bool                          │ │
│  └──────────────────┘   └──────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

#### 3.3.1 Schema Generation Pipeline

```
func signature ─── inspect.signature() ──> Parameter list
                 │
func annotations── getattr(func, '__annotations__', None) or {} ──> Type hints
                 │
                 v
           create_model(name, **fields)
                 │
                 v
           model_json_schema() ──> JSON Schema dict
```

The `or {}` guard handles edge cases: mocks, built-ins, and callables without
`__annotations__`. Both `_generate_schema` (decorator) and `_build_input_model`
(validator) share this defensive pattern.

### 3.4 Castor Stream (`stream/`)

The checkpoint/replay scheduler -- the heart of the kernel.

```
┌───────────────────────────────────────────────────────────────────────┐
│                         Castor Stream                                │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │                       SyscallProxy                               │ │
│  │  (the replay gateway -- injected into every agent function)      │ │
│  ├──────────────────────────────────────────────────────────────────┤ │
│  │ syscall(tool_name, arguments) -> Any                             │ │
│  │ is_replaying: bool (property)                                   │ │
│  │ _handle_spawn(req, args) -> Any                                  │ │
│  │ _handle_spawn_async(req, args) -> str                            │ │
│  │ _handle_join(req, args) -> Any                                   │ │
│  │ _propagate_child_suspension(req, child_cp)                      │ │
│  │ _append_record(record)                                          │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌────────────────┐ ┌────────────────┐ ┌─────────────────────────┐   │
│  │  AgentRunner   │ │  HITLHandler   │ │    AgentRegistry        │   │
│  ├────────────────┤ ├────────────────┤ ├─────────────────────────┤   │
│  │ run()          │ │ approve()      │ │ register(name, fn)      │   │
│  │ run_as_task()  │ │ reject()       │ │ get(name) -> fn          │   │
│  │ preempt()      │ │ modify()       │ │ has_agent(name)         │   │
│  │                │ │ is_child_hitl()│ │ list_agents()           │   │
│  │                │ │ approve/reject/│ │ @castor_agent decorator │   │
│  │                │ │  modify_child  │ │                         │   │
│  └────────────────┘ └────────────────┘ └─────────────────────────┘   │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │                    CheckpointStore                               │ │
│  ├──────────────────────────────────────────────────────────────────┤ │
│  │ save(checkpoint), load(pid), delete(pid), list_pids()           │ │
│  │ SQLAlchemy + SQLite, table: checkpoints(pid, data, updated_at)  │ │
│  └──────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

#### 3.4.1 SyscallProxy -- Internal Architecture

**Constructor parameters:**

| Parameter | Type | Description |
|---|---|---|
| `checkpoint` | `AgentCheckpoint` | The checkpoint being executed |
| `dam` | `CastorDam` | Validation and execution engine |
| `capability_manager` | `CapabilityManager` | Budget enforcement |
| `lodge` | `CastorLodge \| None` | Context eviction (optional) |
| `llm_tool_names` | `set[str] \| None` | Tools triggering Lodge hook (default: `{"llm_inference"}`) |
| `kernel_tool_names` | `set[str] \| None` | Tools auto-skipped during replay (e.g. `{"sys_kernel_page_out"}`) |
| `agent_registry` | `AgentRegistry \| None` | Required for spawn/join operations |

**Internal state:**

| Field | Type | Persisted? | Description |
|---|---|---|---|
| `_replay_index` | `int` | No | Current position in `syscall_log` |
| `_async_tasks` | `dict[str, Task]` | No | Live async child tasks |
| `_async_checkpoints` | `dict[str, AgentCheckpoint]` | No | Live async child checkpoints |

**`is_replaying` property:** Returns `True` when `_replay_index < len(syscall_log)`.

#### 3.4.2 Syscall Execution Pipeline

```
                           proxy.syscall(tool_name, args)
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 1: Lodge eviction hook                │
                 │ (pre-LLM, live only)                       │
                 │ if lodge && !replaying && tool in llm_names│
                 │   -> lodge.check_and_evict()                │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 2: Skip kernel tool records           │
                 │ (replay only)                              │
                 │ while record.tool_name in kernel_tools:    │
                 │   _replay_index++                          │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 3: Replay cache hit                   │
                 │ if replaying:                              │
                 │   match? -> return cached response          │
                 │   mismatch? -> ReplayDivergenceError        │
                 └─────────────────────┬──────────────────────┘
                                       │ (past replay)
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 4: Spawn/Join intercept               │
                 │ if spawn_agent / spawn_agent_async /       │
                 │    join_agent -> handle internally          │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 5: Dam validation                     │
                 │ validate(tool_name, args)                  │
                 │ on ValidationError -> log + return error    │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 6: HITL gate                          │
                 │ if requires_hitl or destructive:           │
                 │   set pending_hitl, raise SuspendInterrupt │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 7: Capability deduction               │
                 │ deduct(caps, resource, cost)               │
                 │ on exhausted -> log + return error          │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 8: Tool execution + refund safety     │
                 │ try: result = dam.execute()                │
                 │ except: refund + re-raise                  │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴──────────────────────┐
                 │ Step 9: Log and return                     │
                 │ _append_record(SyscallRecord(...))         │
                 │ return result                              │
                 └────────────────────────────────────────────┘
```

#### 3.4.3 Sub-Agent Spawning Architecture

Three kernel-internal syscall names, routed directly in `SyscallProxy`:

**Synchronous spawn (`spawn_agent`):**

```
Parent calls proxy.syscall("spawn_agent", {agent_name, capabilities})
  │
  ├─ 1. Look up agent function in AgentRegistry
  ├─ 2. Delegate capabilities: parent -= amount, child = amount
  ├─ 3. Generate deterministic child PID:
  │      "{parent_pid}::{agent_name}-{N}"
  │      where N counts ALL spawn records (sync + async) in parent log
  ├─ 4. Create child AgentCheckpoint(parent_pid=parent.pid)
  ├─ 5. Create child SyscallProxy (shares Dam, CapMgr, Lodge, Registry)
  ├─ 6. await agent_fn(child_proxy)  [BLOCKS until child completes/suspends]
  │      │
  │      ├─ Child SuspendInterrupt -> propagate -> parent suspends
  │      ├─ Child exception -> reclaim delegated budget -> re-raise
  │      └─ Child completes -> continue
  ├─ 7. Reclaim unused child budget
  └─ 8. Log SyscallRecord(response=result, child_checkpoint=child_cp)
```

**Asynchronous spawn (`spawn_agent_async`):**

```
Parent calls proxy.syscall("spawn_agent_async", {agent_name, capabilities})
  │
  ├─ Steps 1-4: same as sync
  ├─ 5. Wrap child in asyncio.create_task(_run_child())
  │      _run_child catches SuspendInterrupt (doesn't re-raise)
  │      _run_child sets child_cp.status = "FAILED" on exception
  ├─ 6. Store task + checkpoint in _async_tasks / _async_checkpoints
  ├─ 7. Budget reclaim guard: try/except reclaims on any failure after delegation
  └─ 8. Log SyscallRecord(response=child_pid), return child_pid handle
```

**Join (`join_agent`):**

```
Parent calls proxy.syscall("join_agent", {handle: child_pid})
  │
  ├─ 1. Look up task and checkpoint by handle
  ├─ 2. await task  [BLOCKS until child completes]
  ├─ 3. Clean up _async_tasks / _async_checkpoints
  ├─ 4. If child status is SUSPENDED_FOR_HITL:
  │      -> propagate -> parent suspends
  ├─ 5. Else: reclaim unused budget
  └─ 6. Log SyscallRecord(response=result, child_checkpoint=child_cp)
```

**Child HITL propagation flow:**

```
Child hits destructive tool
  -> child proxy sets pending_hitl, raises SuspendInterrupt
  -> parent's _handle_spawn catches SuspendInterrupt
  -> _propagate_child_suspension:
      1. Log SyscallRecord(request=spawn_req, response=None, child_cp)
      2. Set parent pending_hitl = {tool_name, arguments, child_pid}
      3. Set parent status = SUSPENDED_FOR_HITL
  -> parent raises SuspendInterrupt
  -> both checkpoints persisted to SQLite
```

#### 3.4.4 AgentRunner -- Execution Model

```
┌────────────────────────────────────────────────────────┐
│              AgentRunner.run(agent_fn, cp)              │
│                                                        │
│  cp.status = "RUNNING"                                 │
│  proxy = SyscallProxy(cp, dam, cap_mgr, lodge, ...)    │
│                                                        │
│  try:                                                  │
│      cp.result = await agent_fn(proxy)                 │
│      cp.status = "COMPLETED"            <-- Happy path │
│  except SuspendInterrupt:                              │
│      pass   <-- Cooperative suspend (cp set by proxy)  │
│  except CancelledError:                                │
│      cp.status = "PREEMPTED"            <-- Preemption │
│      raise                                             │
│                                                        │
│  return cp                                             │
└────────────────────────────────────────────────────────┘
```

- `run_as_task()` wraps `run()` in `asyncio.create_task()`
- `preempt(reason, payload)` sets context on checkpoint, calls `task.cancel()`

#### 3.4.5 HITLHandler -- Feedback Processing

```
                    ┌──────────────────┐
                    │   Human Input    │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              v              v              v
        ┌──────────┐  ┌───────────┐  ┌───────────┐
        │ approve  │  │  reject   │  │  modify   │
        └────┬─────┘  └────┬──────┘  └────┬──────┘
             │              │              │
    validate + deduct  log HITL_     log HITL_
    + execute          REJECTED      MODIFIED
    + log was_hitl     + feedback    + feedback
             │              │              │
              └──────────────┼──────────────┘
                             │
                    clear pending_hitl
                    status = "RUNNING"
                             │
                    ┌────────┴─────────┐
                    │  Replay from top │
                    │  (cached results │
                    │   through proxy) │
                    └──────────────────┘
```

**Child HITL variants** (`approve_child_hitl`, `reject_child_hitl`,
`modify_child_hitl`):

1. Extract child checkpoint from parent's last syscall record
2. Resolve child's pending HITL using standard approve/reject/modify
3. `_resume_child()`: replay child with fresh `SyscallProxy`
4. If child suspends again -> parent stays suspended
5. If child completes -> reclaim budget, update parent to `RUNNING`

#### 3.4.6 CheckpointStore -- Persistence Layer

```
┌──────────────────────────────────────────────┐
│          SQLite (via SQLAlchemy)              │
├──────────────────────────────────────────────┤
│  Table: checkpoints                          │
│  ┌────────────┬──────────┬─────────────────┐ │
│  │ pid (PK)   │ data     │ updated_at      │ │
│  │ String     │ Text     │ DateTime (UTC)  │ │
│  │            │ (JSON)   │                 │ │
│  └────────────┴──────────┴─────────────────┘ │
│                                              │
│  save(cp) -- model_dump_json() -> upsert     │
│  load(pid) -- model_validate_json() -> cp    │
│  delete(pid), list_pids()                    │
└──────────────────────────────────────────────┘
```

### 3.5 Castor Lodge (`lodge/`)

The context window memory management unit (MMU).

```
┌───────────────────────────────────────────────────────────────────────┐
│                         Castor Lodge                                 │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │                      CastorLodge                                 │ │
│  ├──────────────────────────────────────────────────────────────────┤ │
│  │ __init__(registry, driver, token_counter, watermark, ...)       │ │
│  │   Registers: sys_kernel_page_out (kernel-internal)              │ │
│  │   Registers: search_memory (user-facing)                        │ │
│  │                                                                  │ │
│  │ kernel_tool_names -> {"sys_kernel_page_out"}                    │ │
│  │ total_tokens(cp) -> int                                          │ │
│  │ _select_victims(cp) -> list[CastorMessage]                      │ │
│  │ check_and_evict(proxy, cp) -> None                               │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌─────────────────────────┐   ┌─────────────────────────────────┐   │
│  │ SemanticMemoryDriver    │   │    TokenCounter (Protocol)      │   │
│  │ (ABC / HAL)             │   ├─────────────────────────────────┤   │
│  ├─────────────────────────┤   │ count(text) -> int              │   │
│  │ ingest(msgs, pid) -> str│   │                                 │   │
│  │ search(query, pid) -> str│  │ Default: CharCountEstimator    │   │
│  │                         │   │   max(1, len(text) // 4)        │   │
│  │ Impls: InMemoryDriver   │   └─────────────────────────────────┘   │
│  │   (dict + substring)    │                                         │
│  └─────────────────────────┘                                         │
└───────────────────────────────────────────────────────────────────────┘
```

**Eviction flow:**

```
check_and_evict(proxy, checkpoint)
  │
  ├─ total_tokens(cp) <= watermark? -> return (no-op)
  │
  ├─ _select_victims(cp):
  │    FIFO scan of context_history, oldest first
  │    Skip pinned messages (pinned=True)
  │    Collect victims until running_total <= watermark
  │
  ├─ Serialize victims with _pid tag
  │
  ├─ proxy.syscall("sys_kernel_page_out", {messages_json: ...})
  │    -> routes through Dam -> driver.ingest() -> logged in syscall_log
  │
  └─ Remove victims from context_history using id() set matching
     (messages must NOT be copied before eviction)
```

**Replay safety:** Lodge eviction hook fires ONLY during live execution
(`not self.is_replaying`). During replay, `sys_kernel_page_out` records are
auto-skipped by the kernel tool name filter. The checkpoint's
`context_history` is already in its post-eviction state.

### 3.6 LLM Syscall Wrapper (`llm/wrapper.py`)

```
┌─────────────────────────────────────────────────────────┐
│                     LLMSyscall                          │
├─────────────────────────────────────────────────────────┤
│ __init__(registry, call_fn, consumes, cost, tool_name)  │
│   -> Introspects call_fn via _generate_schema()         │
│   -> Registers as ToolMetadata in registry              │
│                                                         │
│ infer(proxy, **kwargs) -> Any                           │
│   -> proxy.syscall(tool_name, kwargs)                   │
│   -> During replay: cached response returned            │
│   -> During live: call_fn executed + logged              │
└─────────────────────────────────────────────────────────┘
```

### 3.7 CLI (`cli.py`)

```
castor [--db path] <command>
  │
  ├── list    -> cmd_list(store, args)
  │     List all checkpoint PIDs with status markers:
  │     HITL | DONE | RUN  | PREM | FAIL
  │
  ├── show <pid>    -> cmd_show(store, args)
  │     Display: PID, status, agent, parent, capabilities, log count,
  │     pending HITL details (including child_pid if spawn HITL)
  │
  ├── reject <pid> --feedback "..."    -> cmd_reject(store, args)
  │     Guards: child HITL check via is_child_hitl()
  │     Calls: handler.reject(cp, feedback) -> store.save(cp)
  │
  └── modify <pid> --feedback "..."    -> cmd_modify(store, args)
        Guards: child HITL check via is_child_hitl()
        Calls: handler.modify(cp, feedback) -> store.save(cp)

Note: `approve` is intentionally excluded -- requires Dam + CapMgr runtime.
```

---

## 4. Data Flow Diagrams

### 4.1 Normal Syscall (Fast Path)

```
Agent Function                SyscallProxy               Dam          CapabilityMgr
     |                            |                       |                |
     |  proxy.syscall("read", {}) |                       |                |
     |--------------------------->|                       |                |
     |                            |                       |                |
     |                    [is_replaying?]                  |                |
     |                    [  NO -- live  ]                 |                |
     |                            |                       |                |
     |                            | validate("read", {})  |                |
     |                            |---------------------->|                |
     |                            |   validated args      |                |
     |                            |<----------------------|                |
     |                            |                       |                |
     |                    [requires_hitl? NO]              |                |
     |                    [destructive?   NO]              |                |
     |                            |                       |                |
     |                            | deduct(caps, "io", 1) |                |
     |                            |-------------------------------------->|
     |                            |                  OK   |                |
     |                            |<--------------------------------------|
     |                            |                       |                |
     |                            | execute("read", args) |                |
     |                            |---------------------->|                |
     |                            |      result           |                |
     |                            |<----------------------|                |
     |                            |                       |                |
     |                    [_append_record()]               |                |
     |                            |                       |                |
     |        result              |                       |                |
     |<---------------------------|                       |                |
```

### 4.2 HITL Suspension and Resume (Slow Path)

```
Agent Function     SyscallProxy     Dam     CapMgr     HITLHandler     Store
     |                  |            |         |            |             |
     | syscall("rm",{}) |            |         |            |             |
     |----------------->|            |         |            |             |
     |                  | validate   |         |            |             |
     |                  |----------->|         |            |             |
     |                  |  validated |         |            |             |
     |                  |<-----------|         |            |             |
     |                  |            |         |            |             |
     |          [destructive? YES]   |         |            |             |
     |                  |            |         |            |             |
     |          set pending_hitl     |         |            |             |
     |          set SUSPENDED_FOR_HITL         |            |             |
     |                  |            |         |            |             |
     |  SuspendInterrupt|            |         |            |             |
     |<-----------------+            |         |            |             |
     |                  |            |         |            |             |
     |  (stack unwinds to AgentRunner)         |            |             |
     |                  |            |         |            | save(cp)    |
     |                  |            |         |            |------------>|
     |                  |            |         |            |             |
     ===== PROCESS SUSPENDED ===== HUMAN DECIDES =====     |             |
     |                  |            |         |            |             |
     |                  |            |         | approve()  |             |
     |                  |            |         |<-----------|             |
     |                  |            |         |            |             |
     |                  |     validate + deduct + execute   |             |
     |                  |            |<------->|<---------->|             |
     |                  |            |         |            |             |
     |                  |       append SyscallRecord (was_hitl=True)      |
     |                  |       clear pending_hitl, set RUNNING           |
     |                  |            |         |            |             |
     ===== REPLAY: agent function re-run from top =====    |             |
     |                  |            |         |            |             |
     | syscall("read",{})|           |         |            |             |
     |----------------->|            |         |            |             |
     |          [replay: cached]     |         |            |             |
     |        result    |            |         |            |             |
     |<-----------------|            |         |            |             |
     |                  |            |         |            |             |
     | syscall("rm",{}) |            |         |            |             |
     |----------------->|            |         |            |             |
     |          [replay: cached (was_hitl)]    |            |             |
     |        result    |            |         |            |             |
     |<-----------------|            |         |            |             |
     |                  |            |         |            |             |
     | (continues live) |            |         |            |             |
```

### 4.3 Sub-Agent Spawn (Synchronous)

```
Parent Agent       Parent Proxy       CapMgr       Child Proxy       Child Agent
     |                  |               |               |                 |
     | syscall           |               |               |                 |
     | ("spawn_agent",  |               |               |                 |
     |  {agent_name:    |               |               |                 |
     |   "researcher"}) |               |               |                 |
     |----------------->|               |               |                 |
     |                  |               |               |                 |
     |          _handle_spawn()         |               |                 |
     |                  |               |               |                 |
     |                  | delegate()    |               |                 |
     |                  |-------------->|               |                 |
     |                  |  child_caps   |               |                 |
     |                  |<--------------|               |                 |
     |                  |               |               |                 |
     |          PID = parent::researcher-0              |                 |
     |          create child AgentCheckpoint            |                 |
     |          create child SyscallProxy               |                 |
     |                  |               |               |                 |
     |                  | await agent_fn(child_proxy)   |                 |
     |                  |------------------------------>|                 |
     |                  |               |               |  child runs... |
     |                  |               |               |<--------------->
     |                  |               |               |                 |
     |                  |               |  child result |                 |
     |                  |<------------------------------|                 |
     |                  |               |               |                 |
     |                  | reclaim()     |               |                 |
     |                  |-------------->|               |                 |
     |                  |               |               |                 |
     |          _append_record(child_checkpoint)        |                 |
     |                  |               |               |                 |
     |  child result    |               |               |                 |
     |<-----------------|               |               |                 |
```

### 4.4 Sub-Agent Spawn (Asynchronous Fan-Out / Fan-In)

```
Parent Agent            Parent Proxy                 CapMgr
     |                       |                         |
     | syscall("spawn_agent_async", {agent: "A"})      |
     |---------------------->|                         |
     |                       | delegate()              |
     |                       |------------------------>|
     |                       | create_task(child_A)    |
     |    handle_A           |                         |
     |<----------------------|                         |
     |                       |                         |
     | syscall("spawn_agent_async", {agent: "B"})      |
     |---------------------->|                         |
     |                       | delegate()              |
     |                       |------------------------>|
     |                       | create_task(child_B)    |
     |    handle_B           |                         |
     |<----------------------|                         |
     |                       |                         |
     |  (parent continues, children run concurrently)  |
     |                       |                         |
     | syscall("join_agent", {handle: handle_A})       |
     |---------------------->|                         |
     |                       | await task_A            |
     |                       | reclaim(child_A_caps)   |
     |                       |------------------------>|
     |   result_A            |                         |
     |<----------------------|                         |
     |                       |                         |
     | syscall("join_agent", {handle: handle_B})       |
     |---------------------->|                         |
     |                       | await task_B            |
     |                       | reclaim(child_B_caps)   |
     |                       |------------------------>|
     |   result_B            |                         |
     |<----------------------|                         |
```

### 4.5 Lodge Eviction During LLM Call

```
Agent Function     SyscallProxy       CastorLodge       Driver       Dam
     |                  |                  |               |           |
     | syscall           |                  |               |           |
     | ("llm_inference", |                  |               |           |
     |  {prompt: "..."}) |                  |               |           |
     |----------------->|                  |               |           |
     |                  |                  |               |           |
     |          [is_replaying? NO]         |               |           |
     |          [tool in llm_tool_names?]  |               |           |
     |          [YES -- trigger hook]      |               |           |
     |                  |                  |               |           |
     |                  | check_and_evict()|               |           |
     |                  |----------------->|               |           |
     |                  |                  |               |           |
     |                  |          [total_tokens > watermark?]         |
     |                  |          [YES]   |               |           |
     |                  |                  |               |           |
     |                  |          _select_victims()       |           |
     |                  |          (FIFO, skip pinned)     |           |
     |                  |                  |               |           |
     |                  | <-- proxy.syscall("sys_kernel_page_out", {}) |
     |                  |                  |               |           |
     |                  |          [SyscallProxy routes    |           |
     |                  |           through Dam normally]  |           |
     |                  |                  |               |           |
     |                  |                  | validate+exec |           |
     |                  |                  |-------------->|---------->|
     |                  |                  |   driver.ingest()         |
     |                  |                  |               |           |
     |                  |                  | confirmation  |           |
     |                  |                  |<--------------|           |
     |                  |                  |               |           |
     |                  |          remove victims from     |           |
     |                  |          context_history         |           |
     |                  |          (id() set matching)     |           |
     |                  |                  |               |           |
     |                  |       eviction done              |           |
     |                  |<-----------------|               |           |
     |                  |                  |               |           |
     |          [continue with LLM syscall normally]      |           |
     |          [validate -> deduct -> execute -> log]     |           |
     |                  |                  |               |           |
     |        LLM result|                  |               |           |
     |<-----------------|                  |               |           |
```

---

## 5. Cross-Cutting Concerns

### 5.1 Error Handling Strategy

| Error Type | Handling | User Space Sees |
|---|---|---|
| `ValidationError` (Pydantic) | Caught by Dam, formatted as `SyscallResponse(VALIDATION_ERROR)` | Structured feedback for self-correction |
| `CapabilityExhaustedError` | Caught by proxy, formatted as `SyscallResponse(INSUFFICIENT_CAPABILITY)` | Feedback message with budget details |
| `ToolNotFoundError` | Raised from `ToolRegistry.get()` | Python exception (programming error) |
| `ReplayDivergenceError` | Raised by proxy on request mismatch | Python exception (corruption detected) |
| `SuspendInterrupt` | Caught by `AgentRunner.run()` | Agent function stack unwound silently |
| `CancelledError` | Caught by `AgentRunner.run()`, budget refunded | Agent function stack unwound, status = PREEMPTED |
| Tool execution exception | Budget refunded by proxy, exception re-raised | Python exception propagated |

### 5.2 Replay Determinism Guarantees

1. Given identical `syscall_log`, replaying the agent function from the top
   produces the same syscall sequence. Any divergence -> `ReplayDivergenceError`.
2. LLM responses are cached in the syscall log. During replay, the LLM provider
   is never called -- the cached response is returned by the proxy.
3. Kernel tools (`sys_kernel_page_out`) are auto-skipped during replay.
   Their side effects are already baked into the checkpoint's `context_history`.
4. `HITL_MODIFIED` responses preserve the original request in the log. On replay,
   the LLM sees the feedback and re-plans -- never mutating `pending_hitl` directly.
5. Preemption context (reason, payload, partial_work) is metadata outside
   `syscall_log` -- does not affect replay determinism.

### 5.3 Budget Conservation Invariants

1. `delegate() + reclaim()` is a closed system. Budget delegated to a child
   is returned on completion via `reclaim(parent_caps, child_caps)`.
2. Refund-on-exception in proxy: if tool execution fails (`BaseException`),
   the deducted budget is refunded before re-raising. Without this, the
   un-logged syscall would be re-attempted on replay, causing a double-deduct.
3. Async spawn budget guard: the delegation -> task creation sequence is wrapped
   in try/except -- if anything fails after delegation, the budget is reclaimed.

---

## 6. Dependency Graph

```
models/capability.py  <---  capability/manager.py
models/checkpoint.py  <---  stream/proxy.py
                      <---  stream/hitl.py
                      <---  stream/runner.py
                      <---  stream/persistence.py
                      <---  lodge/core.py

dam/registry.py       <---  dam/decorator.py
                      <---  dam/validator.py
                      <---  lodge/core.py
                      <---  llm/wrapper.py

dam/validator.py      <---  stream/proxy.py
                      <---  stream/hitl.py
                      <---  stream/runner.py

capability/manager.py <---  stream/proxy.py
                      <---  stream/hitl.py
                      <---  stream/runner.py

lodge/driver.py       <---  lodge/core.py  (ABC only)
                      <---  lodge/drivers/mock_driver.py

lodge/core.py         <---  stream/proxy.py  (TYPE_CHECKING only)
                      <---  stream/hitl.py   (TYPE_CHECKING only)
                      <---  stream/runner.py  (TYPE_CHECKING only)

stream/proxy.py       <---  stream/runner.py
                      <---  stream/hitl.py  (_resume_child imports)
                      <---  llm/wrapper.py

stream/agent_registry.py <--- stream/proxy.py (TYPE_CHECKING only)
                         <--- stream/hitl.py  (TYPE_CHECKING only)
                         <--- stream/runner.py (TYPE_CHECKING only)
```

`TYPE_CHECKING` imports are used for `CastorLodge` and `AgentRegistry`
references in Stream modules to avoid circular imports at runtime.

---

## 7. Test Architecture

170 tests across 14 test files, mapping to kernel subsystems:

| Test File | Module Under Test | Tests | Focus |
|---|---|---|---|
| `test_capability.py` | `capability/manager.py` | 23 | create, check, deduct, refund, delegate, reclaim, errors, atomicity |
| `test_dam_registry.py` | `dam/registry.py` | 7 | register, get, has_tool, list_tools, ToolNotFoundError |
| `test_dam_decorator.py` | `dam/decorator.py` | 9 | schema generation, required/optional params, async detection, registry target |
| `test_dam_validator.py` | `dam/validator.py` | 8 | validate, execute, format_validation_error, sync/async dispatch |
| `test_proxy.py` | `stream/proxy.py` | 12 | replay, divergence, fast path, HITL suspend, capability gating, refund |
| `test_hitl.py` | `stream/hitl.py` | 8 | approve, reject, modify, error states |
| `test_runner.py` | `stream/runner.py` | 5 | run, run_as_task, preempt, CancelledError handling |
| `test_spawn.py` | `stream/proxy.py` spawn + `agent_registry.py` | 30 | sync spawn, async spawn/join, child HITL, budget delegation/reclaim, PID generation, mixed-spawn tests |
| `test_persistence.py` | `stream/persistence.py` | 8 | save, load, delete, list_pids, upsert, CheckpointNotFoundError |
| `test_replay_determinism.py` | LLM replay integration | 3 | LLMSyscall replay, divergence detection, multi-syscall determinism |
| `test_lodge.py` | `lodge/` | 14 | watermark, eviction, pinned messages, token counting, driver ingest/search |
| `test_lodge_integration.py` | `lodge/` + `stream/proxy.py` | 3 | end-to-end eviction through proxy, kernel tool skip during replay |
| `test_e2e.py` | Full kernel integration | 8 | multi-step agents, HITL round-trips, preemption, capability enforcement |
| `test_cli.py` | `cli.py` | 11 | list, show, reject, modify commands, child HITL guard, error handling |
| | **Total** | **170** | |

**Test infrastructure:**

- `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`)
- In-memory SQLite (`sqlite:///:memory:`) for persistence tests
- `InMemoryDriver` for Lodge tests
- Mock LLM callables for replay determinism tests
- No external service dependencies -- all tests are self-contained

---

## Appendix A: Key Invariants

1. **Replay determinism:** Given the same `syscall_log`, replaying the agent
   function from the top must produce the identical syscall sequence. Any
   divergence raises `ReplayDivergenceError`.

2. **Budget conservation:** `delegate() + reclaim()` is a closed system.
   Budget delegated to a child is reclaimed on completion. Refund-on-exception
   prevents leaks from failed tool executions.

3. **HITL immutability:** `pending_hitl` arguments are never mutated. Modify
   logs `HITL_MODIFIED` with feedback; the LLM re-plans on replay.

4. **Kernel tool transparency:** Records for kernel-internal tools
   (`sys_kernel_page_out`) are auto-skipped during replay. Their side effects
   are baked into the checkpoint state.

5. **Lodge replay safety:** `check_and_evict()` only fires during live
   execution. During replay, eviction records are skipped and `context_history`
   is already in its post-eviction state.

6. **No direct side effects:** Agent functions must route all non-deterministic
   operations through `proxy.syscall()`. Direct calls bypass the log and break
   replay.

7. **PID determinism:** Child PIDs are generated as `{parent}::{name}-{N}`
   where N counts ALL prior spawn records (both sync and async) to prevent
   collision between spawn modes.

## Appendix B: Public API Surface

20 symbols exported from `castor.__init__`:

| Symbol | Module | Category |
|---|---|---|
| `AgentCheckpoint` | `models.checkpoint` | Data model |
| `CastorMessage` | `models.checkpoint` | Data model |
| `SyscallRecord` | `models.checkpoint` | Data model |
| `SuspendInterrupt` | `models.checkpoint` | Exception |
| `Capability` | `models.capability` | Data model |
| `SyscallRequest` | `models.capability` | Data model |
| `SyscallResponse` | `models.capability` | Data model |
| `CapabilityManager` | `capability.manager` | Kernel subsystem |
| `CastorDam` | `dam.validator` | Kernel subsystem |
| `castor_tool` | `dam.decorator` | Decorator |
| `SyscallProxy` | `stream.proxy` | Core gateway |
| `AgentRunner` | `stream.runner` | Kernel subsystem |
| `HITLHandler` | `stream.hitl` | Kernel subsystem |
| `AgentRegistry` | `stream.agent_registry` | Registry |
| `AgentNotFoundError` | `stream.agent_registry` | Exception |
| `castor_agent` | `stream.agent_registry` | Decorator |
| `CheckpointStore` | `stream.persistence` | Persistence |
| `CastorLodge` | `lodge.core` | Kernel subsystem |
| `LLMSyscall` | `llm.wrapper` | LLM integration |

## Appendix C: Configuration & Build

```toml
# pyproject.toml essentials
[project]
name = "castor"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pydantic>=2.0", "sqlalchemy>=2.0"]

[project.scripts]
castor = "castor.cli:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP"]
```
