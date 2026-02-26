# Detailed Design Document (DDD): Project Castor

## 1. System Architecture: Space Isolation

Castor implements a strict **Microkernel Architecture**. The system is physically and logically divided into two spaces:

- **User Space (Untrusted):** Contains the LLM inference endpoint, prompt templates, and the client UI (e.g., a terminal app or web interface). Code here cannot directly access the file system, network, or other processes.

- **Kernel Space (Trusted):** The Castor Engine. It manages resources, capabilities, process scheduling, and actual tool execution.

- **The Bridge (Syscall Interface):** User Space communicates with Kernel Space exclusively via a standardized `SyscallRequest`.

## 2. Core Modules & Component Design

### 2.1 Capability Manager (The Resource Allocator)

Manages dynamic budgets (Capabilities) assigned to processes. It replaces static Access Control Lists (ACLs).

**Design Pattern:** Token Bucket / Quota System.

**Core Responsibilities:**

- Initialize root capabilities for the Main Agent Process.
- Track resource consumption (e.g., API calls, tokens, money, file deletions).
- **Delegation:** When a process spawns a sub-process, the Capability Manager strictly partitions a subset of the parent's budget for the child.
- Trigger `CapabilityExhaustedInterrupt` if a Syscall exceeds the remaining budget.

### 2.2 Castor Dam (Strongly-Typed Tool Sandbox)

The execution boundary. It intercepts all Syscalls and validates them against predefined contracts.

- **Implementation:** Leverages `pydantic` for schema validation and serialization.
- **Decorator `@castor_tool`:** Used by developers to register tools into the kernel. It requires specifying the `consumes` (which capability it drains) and `cost`.
- **Error Handling:** Never throws a Python runtime exception to the User Space. It catches `ValidationError`, formats it into a natural language `SyscallErrorResponse`, and feeds it back to the LLM for self-correction.

### 2.3 Castor Stream (Checkpoint/Replay Scheduler)

The Process Manager. It handles the lifecycle of all Agent processes and sub-processes using an `asyncio` event loop and a **checkpoint/replay** execution model inspired by durable execution engines (Temporal, Azure Durable Functions).

**Why Checkpoint/Replay:** Python `asyncio` coroutines cannot be serialized — they hold C-level stack frames, event loop references, and closures. Instead of pickling the coroutine, Castor Stream records a **replay log** of all completed syscalls. To resume a suspended agent, it re-executes the agent function from the top and serves cached responses for all previously-completed syscalls, fast-forwarding to the suspension point.

- **Agent Checkpoint:** Every Agent (main or sub) is tracked via an `AgentCheckpoint` object — a plain Pydantic model containing the `syscall_log` (the replay journal), capabilities, and any pending HITL request. This model is trivially serializable to JSON.

- **SyscallProxy (The Replay Gateway):** Each agent function receives a `SyscallProxy` instance. The agent calls `await proxy.syscall(tool_name, arguments)` for all interactions. The proxy decides:
  1. **Replay:** If `replay_index < len(syscall_log)`, return the cached response instantly. The agent function does not know the difference.
  2. **New + Fast Path:** If the syscall is new and safe, route through Dam → Capability Manager → execute. Append `(request, response)` to the log.
  3. **New + Slow Path:** If the syscall is new and destructive/over-budget, set `pending_hitl` on the checkpoint and raise `SuspendInterrupt` to unwind the coroutine stack.

- **Suspend (The Slow Path):**
  `SuspendInterrupt` is a Python exception that propagates up, tearing down the coroutine. The coroutine is **destroyed**, not preserved. Stream catches the exception at the top level, serializes `checkpoint.model_dump_json()` to SQLite, and dispatches a webhook/CLI prompt to the human.

- **Resume (Replay):**
  Upon receiving human feedback (approve, reject, or modify), Stream:
  1. Loads the `AgentCheckpoint` from SQLite.
  2. Processes the human decision (see Section 5: HITL Feedback Loop).
  3. Re-runs the agent function from the top with a fresh `SyscallProxy` loaded with the existing `syscall_log`.
  4. The proxy fast-forwards through all cached syscalls, then continues live execution from the point after suspension.

- **Determinism Guarantee:** On replay, the agent function must produce the same syscall sequence. This is naturally satisfied because the LLM is never re-called during replay — its decisions are captured as syscall requests in the log. Only the `SyscallProxy` is invoked, and it serves cached responses.

- **Sub-Agent Spawning:** Handles `spawn_agent` and `spawn_agent_async` syscalls. Each child gets its own `AgentCheckpoint` with an isolated `syscall_log`. The child's final result becomes the response of the `spawn_agent` syscall in the parent's log (see Section 4).

### 2.4 Castor Lodge (Context MMU - Memory Management Unit)

Manages the LLM's Context Window to prevent amnesia and Out-Of-Memory (OOM) errors.

**Core Responsibilities:**

- Count tokens for the current `AgentCheckpoint` context.
- **Pinning:** Ensure System Instructions (core OS rules) are locked in "VRAM" (top of the prompt) and never evicted.
- **Paging Out:** When the context reaches a warning threshold (e.g., 90% of max tokens), trigger a background task to summarize or vectorize the oldest unlocked message blocks and save them to a local vector store.

## 3. Core Data Models (Python / Pydantic Blueprints)

These are the foundational data structures the Coding Agent must implement first.

### 3.1 Capabilities & Syscalls

```python
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Literal

class Capability(BaseModel):
    resource_type: str  # e.g., "network_read", "disk_delete", "api_usd"
    max_budget: float
    current_usage: float = 0.0

class SyscallRequest(BaseModel):
    caller_pid: str
    tool_name: str
    arguments: Dict[str, Any]

class SyscallResponse(BaseModel):
    status: Literal["SUCCESS", "VALIDATION_ERROR", "HITL_MODIFIED", "HITL_REJECTED",
                     "SUSPENDED", "INSUFFICIENT_CAPABILITY"]
    result_payload: Optional[Any] = None
    feedback_message: Optional[str] = None  # Used to guide LLM self-correction
    human_feedback: Optional[str] = None    # Natural language from HITL reviewer
```

### 3.2 Replay Log & Agent Checkpoint

The checkpoint/replay model replaces coroutine serialization. The replay log is the sole source of truth for an agent's execution history.

```python
class SyscallRecord(BaseModel):
    """One completed syscall and its result, stored in the replay log."""
    request: Dict[str, Any]            # {"tool_name": ..., "arguments": ...}
    response: Any                      # The tool's return value or HITL feedback
    was_hitl: bool = False             # Whether this syscall went through Slow Path
    child_checkpoint: Optional["AgentCheckpoint"] = None  # Nested, for spawn_agent

class AgentCheckpoint(BaseModel):
    """
    The entire serializable state of an agent process.
    Replaces AgentProcess — no coroutine frames, no closures.
    This IS the process. Trivially serializable via model_dump_json().
    """
    pid: str
    parent_pid: Optional[str] = None
    status: Literal["RUNNING", "SUSPENDED_FOR_HITL", "COMPLETED", "FAILED"]
    agent_function_name: str           # Registry key to look up the function on replay
    capabilities: Dict[str, Capability]
    syscall_log: List[SyscallRecord] = []              # The replay journal
    pending_hitl: Optional[Dict[str, Any]] = None      # The blocked syscall (Slow Path)
    context_history: List[Dict[str, Any]] = []         # LLM message history (for Lodge)
```

### 3.3 Suspend Interrupt

```python
class SuspendInterrupt(Exception):
    """
    Raised by SyscallProxy to unwind the coroutine stack when HITL is needed.
    The coroutine is destroyed, NOT preserved. The checkpoint survives.
    """
    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
```

### 3.4 Syscall Proxy (The Replay Gateway)

```python
class SyscallProxy:
    """
    Injected into every agent function. All side effects go through this proxy.
    It decides: replay from cache, execute live (Fast Path), or suspend (Slow Path).
    """
    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
        self._replay_index = 0

    async def syscall(self, tool_name: str, arguments: dict) -> Any:
        request = {"tool_name": tool_name, "arguments": arguments}

        # ── Replay: return cached response instantly ──
        if self._replay_index < len(self.checkpoint.syscall_log):
            record = self.checkpoint.syscall_log[self._replay_index]
            assert record.request == request, (
                f"Replay divergence at index {self._replay_index}: "
                f"expected {record.request}, got {request}"
            )
            self._replay_index += 1
            return record.response

        # ── New syscall: route through Dam → Capability Manager ──
        validated = castor_dam.validate(tool_name, arguments)  # Pydantic check
        tool_meta = castor_dam.get_tool_meta(tool_name)

        if tool_meta.requires_hitl or tool_meta.is_destructive:
            self.checkpoint.pending_hitl = request
            self.checkpoint.status = "SUSPENDED_FOR_HITL"
            raise SuspendInterrupt(self.checkpoint)

        capability_manager.deduct(self.checkpoint.capabilities, tool_meta)
        result = await castor_dam.execute(validated)

        self.checkpoint.syscall_log.append(SyscallRecord(
            request=request, response=result
        ))
        return result
```

### 3.5 Tool Registration Interface

```python
from typing import Callable

def castor_tool(consumes: str, cost_per_use: float = 1.0,
                requires_hitl: bool = False, destructive: bool = False):
    """
    Kernel decorator to register a Python function as a secure Syscall.
    """
    def decorator(func: Callable):
        # 1. Inspect function signature and generate Pydantic Schema
        # 2. Register into Castor Dam Registry with capability requirements
        # 3. Tag with requires_hitl / destructive metadata for Fast/Slow routing
        return func
    return decorator
```

## 4. Kernel Workflow: Sub-Agent Spawning & IPC

Under the checkpoint/replay model, `spawn_agent` is a syscall like any other — but the kernel handles it internally by creating a nested checkpoint. Two modes are supported.

### 4.1 Synchronous Spawn (Blocking)

The parent agent blocks until the child completes. The child's result becomes the return value of the parent's `spawn_agent` syscall.

1. **Request:** The parent agent function calls `await proxy.syscall("spawn_agent", {"role": "researcher", "requested_caps": {"network_read": 10}})`.

2. **Kernel Intercept:** Castor Dam validates the spawn schema.

3. **Capability Delegation:** Capability Manager checks if the parent has enough `network_read` budget. If yes, it deducts 10 units from the parent and creates a new `Capability` set for the child.

4. **Child Checkpoint Creation:** Castor Stream creates a new `AgentCheckpoint` with a unique PID, the delegated capabilities, an empty `syscall_log`, and resolves `agent_function_name` from the `role` to a registered agent function.

5. **Child Execution:** Stream runs the child agent function with its own `SyscallProxy`. The child's syscalls go through the same Dam → Capability Manager → execute pipeline, recorded in the child's own log.

6. **Child Completion & Capability Reclamation:** Once the child function returns, Stream destroys the child process, reclaims unused capability budget (returns it to the parent), and captures the child's return value.

7. **Parent Log Entry:** The parent's `SyscallProxy` records the result:

```python
SyscallRecord(
    request={"tool_name": "spawn_agent", "arguments": {"role": "researcher", ...}},
    response={"policy": "90-day retention"},    # Child's return value
    child_checkpoint=AgentCheckpoint(           # Nested — the child's full log
        pid="child-001",
        syscall_log=[...],                      # All of the child's syscalls
        status="COMPLETED"
    )
)
```

On **replay**, when the parent re-executes and hits `proxy.syscall("spawn_agent", ...)`, the proxy returns the cached child result instantly — the child function is **not** re-run.

### 4.2 Child Suspension (HITL Propagation)

If the child hits a destructive syscall and raises `SuspendInterrupt`, the suspension propagates to the parent:

1. The child's proxy sets `pending_hitl` and raises `SuspendInterrupt`.
2. The kernel catches it within the `spawn_agent` handler.
3. The parent's `spawn_agent` syscall is also blocked — the parent's proxy records the child's suspended checkpoint in `pending_hitl` and raises `SuspendInterrupt` for the parent.
4. Both checkpoints are persisted to SQLite (the child's nested inside the parent's).

On resume, the kernel processes the human decision on the child's pending syscall first, replays the child to completion, then replays the parent with the child's result now cached.

### 4.3 Asynchronous Spawn (Non-Blocking Fan-Out/Fan-In)

For parallel child agents, `spawn_agent_async` returns immediately with a handle. The parent later collects results via `join_agent`:

```python
async def orchestrator_agent(proxy: SyscallProxy):
    # Fan-out: launch 3 children in parallel
    h1 = await proxy.syscall("spawn_agent_async", {"role": "researcher", "task": "weather in Bellevue"})
    h2 = await proxy.syscall("spawn_agent_async", {"role": "researcher", "task": "weather in Seattle"})
    h3 = await proxy.syscall("spawn_agent_async", {"role": "researcher", "task": "weather in Portland"})

    # Parent continues its own work while children run
    notes = await proxy.syscall("read_file", {"path": "notes.txt"})

    # Fan-in: block until each child completes
    r1 = await proxy.syscall("join_agent", {"handle": h1})
    r2 = await proxy.syscall("join_agent", {"handle": h2})
    r3 = await proxy.syscall("join_agent", {"handle": h3})

    return {"weather": [r1, r2, r3], "notes": notes}
```

Internally, `spawn_agent_async` tells Castor Stream to schedule the child on the event loop and returns a handle (a child PID string). `join_agent` awaits the child's completion. Both are regular syscalls — both get logged and are replayable.

## 5. HITL Feedback Loop

When the Slow Path suspends an agent for human review, the human can respond in three ways. The kernel handles each differently to preserve replay log integrity.

### 5.1 Approve (No Modification)

The simplest case. The human approves the pending syscall as-is.

1. Stream loads the `AgentCheckpoint` from SQLite.
2. Stream executes the previously-blocked syscall **now** (Dam re-validates, Capability Manager deducts).
3. The result is appended to `syscall_log` with `was_hitl=True`.
4. `pending_hitl` is cleared, status returns to `RUNNING`.
5. The agent function is **replayed from the top**. All previous syscalls (including the now-completed one) are served from cache. Execution continues live from the next syscall.

### 5.2 Reject

The human rejects the action entirely.

1. Stream loads the checkpoint.
2. The rejected syscall is appended to `syscall_log` with a rejection response:

```python
SyscallRecord(
    request={"tool_name": "delete_emails", "arguments": {"ids": [...]}},
    response={"status": "HITL_REJECTED", "human_feedback": "Too risky, do not delete."},
    was_hitl=True,
)
```

3. `pending_hitl` is cleared.
4. The agent function is replayed. When it reaches the rejected syscall, it receives the rejection as the return value. The LLM sees this feedback in context and must re-plan (e.g., propose a safer alternative or abort gracefully).

### 5.3 Approve with Modification

The human approves the intent but modifies the scope (e.g., "Go ahead, but keep emails from the last 7 days"). This is the critical case.

**The kernel does NOT mutate `pending_hitl` arguments.** Doing so would cause a replay divergence — on replay, the agent function would emit the original request, but the log would contain a modified one, failing the replay assertion.

Instead, the modification is treated as **feedback that triggers LLM re-planning:**

1. Stream loads the checkpoint.
2. The original syscall is appended to `syscall_log` with a modification response:

```python
SyscallRecord(
    request={"tool_name": "delete_emails", "arguments": {"ids": [847_ids]}},
    response={
        "status": "HITL_MODIFIED",
        "human_feedback": "Only delete emails older than 7 days, keep recent ones.",
    },
    was_hitl=True,
)
```

3. `pending_hitl` is cleared.
4. The agent function is replayed. When it reaches the modified syscall, it receives the `HITL_MODIFIED` response. The agent feeds this to the LLM as context. The LLM re-plans and issues a **new** syscall with revised arguments:

```
proxy.syscall("delete_emails", {ids: [847 ids]})  → HITL_MODIFIED (cached, index N)
  ↓ LLM sees feedback, re-plans
proxy.syscall("delete_emails", {ids: [712 ids]})  → NEW syscall (index N+1, live)
```

5. The new syscall is a fresh entry in the log. It goes through Dam → Capability Manager as usual. Depending on kernel policy, it may Fast Path (human already approved the intent) or Slow Path again (if strict re-approval is required for modified arguments).

**Why this approach:**
- **Replay integrity is preserved.** The log accurately records what happened: original attempt → human feedback → revised attempt.
- **The human gives natural language,** not hand-edited JSON. The LLM translates "keep last 7 days" into a revised `ids` list.
- **Full audit trail.** The log shows the original attempt, the human's reasoning, and the LLM's revised action.

## 6. Full Kernel Lifecycle Trace

A complete trace of a multi-agent workflow: "Clean up my inbox and summarize the retention policy."

```
Time   Component         Event
─────  ────────────────  ──────────────────────────────────────────────────
t0     User Space        User submits task to OpenClaw
t1     Castor Stream     Creates AgentCheckpoint(pid="main-001", syscall_log=[])
                         Runs main_agent(proxy)

t2     SyscallProxy      proxy.syscall("list_emails", {older_than: 30})
       ├─ Replay Check   replay_index=0, log empty → NEW syscall
       ├─ Castor Dam     Validates {older_than: int} → OK
       ├─ Cap Manager    cost=1 network_read, budget=100 → OK, deduct → Fast Path
       └─ Execute        list_emails() → 847 emails
       ▸ syscall_log = [{list_emails → 847 emails}]

t3     SyscallProxy      proxy.syscall("spawn_agent", {role: "policy_researcher"})
       ├─ Replay Check   replay_index=1, log has 1 entry → NEW syscall
       ├─ Castor Dam     Validates spawn schema → OK
       ├─ Cap Manager    Deducts 10 network_read from parent (100→90)
       └─ Castor Stream  Creates child checkpoint, runs researcher_agent(child_proxy)

t4       Child Proxy     child_proxy.syscall("web_search", {q: "email retention"})
         ├─ Dam          OK
         ├─ Cap Manager  child budget: 10→9
         └─ Execute      web_search() → "90-day policy"
         ▸ child syscall_log = [{web_search → "90-day policy"}]

t5       Child           researcher_agent returns {"policy": "90 days"}
         Castor Stream   Child COMPLETED. Reclaim 9 unused units → parent: 90→99
       ▸ parent syscall_log = [{list_emails→...}, {spawn_agent→{policy:"90 days"}}]

t6     SyscallProxy      proxy.syscall("delete_emails", {ids: [847 ids]})
       ├─ Replay Check   replay_index=2, log has 2 entries → NEW syscall
       ├─ Castor Dam     Validates {ids: List[str]} → OK
       ├─ Cap Manager    destructive=True → SLOW PATH
       └─ SyscallProxy   Sets pending_hitl, raises SuspendInterrupt
       ▸ checkpoint = {
           syscall_log: [list_emails, spawn_agent],
           pending_hitl: {tool: "delete_emails", args: {ids: [847]}},
           status: "SUSPENDED_FOR_HITL"
         }

t7     Castor Stream     Catches SuspendInterrupt
                         checkpoint.model_dump_json() → SQLite
                         webhook → human: "Agent wants to delete 847 emails"

       ─── 2 hours pass. Human reviews. ───

t8     Webhook           Human responds: "Only delete emails older than 7 days"
       Castor Stream     Loads checkpoint from SQLite
                         Handles as HITL_MODIFIED (Section 5.3):
                           Appends {delete_emails → HITL_MODIFIED} to log
                           Clears pending_hitl
                         ▸ syscall_log = [list_emails, spawn_agent, delete_emails(MODIFIED)]

t9     Castor Stream     REPLAYS main_agent(proxy) from the top
                         ▸ proxy.syscall("list_emails")    → index 0, CACHED
                         ▸ proxy.syscall("spawn_agent")    → index 1, CACHED (child NOT re-run)
                         ▸ proxy.syscall("delete_emails")  → index 2, CACHED → HITL_MODIFIED
                         Agent feeds human feedback to LLM. LLM re-plans.

t10    SyscallProxy      proxy.syscall("delete_emails", {ids: [712 ids]})  ← revised
       ├─ Replay Check   replay_index=3, log has 3 entries → NEW syscall
       ├─ Castor Dam     Validates → OK
       ├─ Cap Manager    Kernel policy: human approved intent → Fast Path
       └─ Execute        delete_emails(712 ids) → OK
       ▸ syscall_log = [..., delete_emails(MODIFIED), delete_emails(712, OK)]

t11    SyscallProxy      proxy.syscall("send_summary", {body: "Deleted 712 emails"})
       ├─ Replay Check   NEW → Fast Path → execute
       └─ Execute        send_summary() → OK

t12    Castor Stream     main_agent returns. status = COMPLETED.
                         Final syscall_log has 5 entries. Full audit trail preserved.
```
