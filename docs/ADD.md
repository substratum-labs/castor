# Architecture Design Document (ADD): Project Castor

## 1. High-Level Architecture Overview

Project Castor adopts a **Microkernel Architecture** adapted for LLM Agents. The system strictly separates the non-deterministic components (the LLM and the UI) from the deterministic execution environment (the kernel).

- **User Space (Untrusted):** Contains the LLM (e.g., OpenAI, Anthropic, local open-source models), Prompt Engineering logic, and the Client UI (e.g., OpenClaw, NanoBot). User Space cannot execute functions directly; it must emit a `SyscallRequest`.

- **Kernel Space (Trusted):** The Castor engine. It receives Syscalls, validates them against strong type contracts, checks capability budgets, and schedules their execution asynchronously.

## 2. Core Components (The Microkernel)

### 2.1 Capability Manager (The IAM Engine)

Manages dynamic budgets (Tokens) assigned to a specific Agent Process.

- **Responsibility:** Tracks and deducts quotas (e.g., `disk_write_budget: 5`, `financial_budget: $50`).
- **Mechanism:** Intercepts every Syscall. If a requested action exceeds the remaining budget, it triggers a `CapabilityExhaustedInterrupt`, forcing the Scheduler to route the request to the Slow Path (Human-in-the-Loop).

### 2.2 Castor Dam (Tool Registry & Validation)

A strongly-typed boundary layer built on top of **Pydantic V2**.

- **Responsibility:** Converts untrusted JSON strings from the LLM into validated Python objects.
- **Mechanism:** Uses a `@castor_tool` decorator. It validates input schemas and registers the required Capability cost for the tool. If validation fails, it generates a standardized `SchemaValidationError` to be fed back to the LLM for self-correction.

### 2.3 Castor Stream (Preemptive Checkpoint/Replay Scheduler)

The heart of the OS, managing the lifecycle of Agent processes using `asyncio`.

- **Responsibility:** Executes and manages agent lifecycle — running, suspending, preempting, and resuming agent processes.
- **Mechanism:** Uses a **checkpoint/replay** execution model. Agent state is a replay log of completed syscalls, not serialized coroutine frames (Python coroutines cannot be pickled). Each agent function receives a `SyscallProxy` that decides whether to serve cached responses (replay) or execute live. On suspension (HITL) or preemption (`asyncio.Task.cancel()`), the `AgentCheckpoint` (a Pydantic model) is persisted to SQLite. Resume re-runs the agent function from the top, fast-forwarding through cached syscalls.

See the DDD Section 2.3 for the full checkpoint/replay design and `docs/PREEMPTIVE_SCHEDULING.md` for the preemption mechanism.

### 2.4 Castor Lodge (Context Pager)

The memory management unit (MMU) for the LLM's Context Window.

- **Responsibility:** Prevents Out-Of-Memory (OOM) errors at the token level.
- **Mechanism:** Monitors the token count. When the threshold is reached, it automatically "pages out" older conversation history to a local Vector DB, while "pinning" absolute core system prompts in VRAM so they are never evicted.

## 3. Execution Flow: The Lifecycle of a Syscall

When the LLM decides to take an action, the following flow occurs:

1. **Syscall Emission:** The LLM outputs a JSON payload specifying the tool and arguments. OpenClaw (User Space) forwards this to `Castor.execute_syscall(payload)`.

2. **Type Validation (Castor Dam):** Pydantic verifies the payload against the registered tool schema.
   - **Failure:** Returns a Type Error directly to the LLM.
   - **Success:** Proceeds to Capability Check.

3. **Capability Check & Routing:** The Capability Manager assesses the cost.
   - **The Fast Path (Automated):** If the tool is safe (e.g., `read_weather`) or within the allocated capability budget, the tool executes immediately in the current asyncio loop. The budget is deducted.
   - **The Slow Path (Preempted):** If the tool is marked as `destructive=True` or exceeds the budget, Castor Stream fires an interrupt.

4. **Suspension & HITL (Slow Path Only):**
   - `SyscallProxy` raises `SuspendInterrupt`, which unwinds the coroutine stack.
   - The `AgentCheckpoint` (containing the `syscall_log` of all completed syscalls) is persisted to SQLite (`status = SUSPENDED_FOR_HITL`).
   - A Webhook or CLI prompt is dispatched to the human.

5. **Resume & Execution:**
   - The human approves (or rejects, or modifies) the action.
   - Castor Stream loads the `AgentCheckpoint` from SQLite and **replays** the agent function from the top. The `SyscallProxy` serves cached responses for all previously-completed syscalls, fast-forwarding to the suspension point, then continues live execution.

## 4. Data Models (Phase 1 - Python)

> **Note:** The DDD (Section 3) is the canonical source of truth for data models. The schemas below are a high-level summary. See `src/castor/models/` for the implementation.

```python
class Capability(BaseModel):
    resource_type: str  # e.g., "email_deletion", "api_spend"
    max_budget: float
    current_usage: float = 0.0

class SyscallRequest(BaseModel):
    caller_pid: str
    tool_name: str
    arguments: dict[str, Any]

class SyscallRecord(BaseModel):
    request: dict[str, Any]     # {"tool_name": ..., "arguments": ...}
    response: Any               # The tool's return value or HITL feedback
    was_hitl: bool = False

class AgentCheckpoint(BaseModel):
    pid: str
    status: Literal["RUNNING", "SUSPENDED_FOR_HITL", "PREEMPTED", "COMPLETED", "FAILED"]
    agent_function_name: str
    capabilities: dict[str, Capability]
    syscall_log: list[SyscallRecord] = []   # The replay journal
    pending_hitl: Optional[dict[str, Any]] = None
```
