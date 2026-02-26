# Product Requirements Document (PRD): Project Castor

## 1. Product Vision

Castor is a lightweight, deterministic "Operating System Kernel (Microkernel)" designed specifically for LLM Agents.

It aims to solve the most fatal engineering pain points in the current Agent ecosystem (e.g., OpenClaw): execution loss of control, privilege abuse, and context state collapse. Castor refuses to pin the execution safety of the system on the non-deterministic attention mechanisms of LLMs. Instead, it "cages" the large language model within a secure physical execution engine by providing a strongly-typed sandbox, capability-based security budgets, and preemptive hardware interrupts.

**One-Line Positioning:** Castor is not another LangChain; it is the L4 secure microkernel for the Agent era.

## 2. Core Philosophy

- **User Space and Kernel Space Isolation:** LLMs (the models themselves) and interactive interfaces (like OpenClaw) run in "user space" and do not have the privilege to directly execute external code or system I/O. They must request resources through System Calls (Syscalls) exposed by Castor.

- **Capability-Driven Security:** Abandons rigid ACLs (Access Control Lists) in favor of dynamic budget management. Tool execution consumes specific "capability tokens" (e.g., financial limits, deletion limits).

- **Fast/Slow Path Separation:** Safe operations within budget are executed instantly (Fast Path); operations hitting high-risk thresholds are forced onto the Slow Path, triggering suspension and manual approval.

- **Preemptive Control Flow:** Human-in-the-loop (HITL) feedback acts as the highest priority interrupt signal, capable of suspending, modifying, or resuming the Agent's execution state at any time.

## 3. Key Subsystems & Functional Requirements

### 3.1 Castor Dam (Tool Registry & Strongly-Typed Sandbox)

**Requirement:** All tools provided to the LLM must be defined with strong typing (based on Pydantic).

**Features:**

- Intercepts and validates the JSON payload output by the LLM before executing any Python function.
- In case of type mismatch, it automatically generates standardized error feedback to the LLM to trigger Self-Correction, rather than causing a system runtime crash.
- Tools must declare the Capability they consume upon registration (e.g., `@castor_tool(consumes="disk_delete", cost=1)`).

### 3.2 Castor Stream (Preemptive Asynchronous Scheduler)

**Requirement:** Replaces traditional `while True` loops to implement controllable coroutine scheduling.

**Features:**

- **State Serialization:** When a high-risk operation is triggered, the engine instantly suspends the current coroutine, packaging and serializing local variables, the chain of thought, and the call stack to disk, freeing up memory.
- **Resume (Breakpoint Continuation):** Upon receiving a Webhook containing human approval results, it deserializes the state snapshot, injects human feedback, and resumes execution.

### 3.3 Castor Lodge (Context & Memory Pager)

**Requirement:** Prevents amnesia caused by LLM Context Window overflow.

**Features:**

- Monitors the Token consumption of the current Prompt.
- When approaching the context threshold, it triggers automatic **Paging Out**: vectorizing early, non-core memories and storing them in a local database (Swap Space).
- **Absolute Pinning** of core system instructions to ensure safety rules and system prompts are never squeezed out of VRAM.

## 4. Implementation Roadmap & Tech Stack

To ensure extremely fast architectural validation iteration and ultimately achieve ultimate low-level performance and memory safety, the project will be executed in two phases:

### Phase 1: High-Fidelity Python Prototype (The Blueprint)

- **Goal:** Rapidly validate business logic, API ergonomics, and state machine flow.
- **Core Dependencies:** Python 3.11+, Pydantic V2 (strongly-typed contracts), asyncio (concurrency and suspension scheduling), SQLite/SQLAlchemy (state persistence).

### Phase 2: Rust Performance Rewrite (The Performance Core)

- **Goal:** Solidify the specifications of the Python prototype and push the core engine down to a memory-safe, system-level language.
- **Core Dependencies:** Rust, Tokio (async runtime), PyO3 + Maturin (compiling into a native Python extension package).

**Final Delivery Format:** Upper-layer developers still use clean Python APIs via `import castor`, but the complex underlying concurrent state locks and serialization are taken over by the Rust microkernel.
