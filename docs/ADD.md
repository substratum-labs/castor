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

### 2.3 Castor Stream (Preemptive Async Scheduler)

The heart of the OS, managing the lifecycle of Agent Tasks using `asyncio`.

- **Responsibility:** Executes the Directed Acyclic Graph (DAG) or State Machine of the agent's workflow.
- **Mechanism:** Supports pausing (suspend) and resuming (resume) coroutines. It serializes the `TaskState` (including local variables, history, and the pending Syscall) to a local SQLite/PostgreSQL database when a hardware interrupt (HITL approval required) occurs.

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
   - The current state is serialized to the database (`status = PENDING_APPROVAL`).
   - Memory is freed. A Webhook or CLI prompt is dispatched to the human.

5. **Resume & Execution:**
   - The human approves the action (injecting a `GrantToken`).
   - Castor Stream deserializes the state, restores the coroutine, and physically executes the Python function.

## 4. Initial Data Models (Phase 1 - Python)

To guide the coding agent, here are the foundational schemas (using pseudo-Python/Pydantic):

```python
class CapabilityToken(BaseModel):
    id: str
    resource_type: str  # e.g., "email_deletion", "api_spend"
    max_budget: float
    current_usage: float = 0.0

class SyscallRequest(BaseModel):
    tool_name: str
    arguments: dict
    process_id: str

class TaskState(BaseModel):
    process_id: str
    status: Literal["RUNNING", "SUSPENDED", "COMPLETED", "FAILED"]
    context_history: list[dict] # The LLM message history
    pending_syscall: Optional[SyscallRequest]
    serialized_locals: bytes    # The pickled or JSON state of the coroutine
```
