# Design Review: Project Castor

Initial architectural review of the PRD, ADD, and DDD before the checkpoint/replay revision.

## Strengths

### 1. The OS Metaphor Maps Well

The kernel/user-space separation is not cosmetic — it maps to a real trust boundary in LLM agent systems:

| OS Concept | Castor Analog | Why it fits |
|---|---|---|
| User/Kernel space | LLM vs. Engine | The trust boundary is real, not metaphorical |
| Syscalls | Tool invocations | LLM can only act through a validated gate |
| Capabilities | Budget tokens | Resource control that degrades gracefully |
| Process scheduling | Agent lifecycle | Suspend/resume maps to HITL naturally |
| Virtual memory/paging | Context window mgmt | Finite "memory" with eviction strategies |

Most agent frameworks (LangChain, CrewAI, etc.) treat the LLM as the *controller*. Castor flips this: the kernel is the controller, and the LLM is just a user-space process that makes requests. That is a meaningful architectural stance.

### 2. Capability-Based Security Is the Right Call

ACLs say "who can do what." Capabilities say "how much can you still do." For agents that burn through resources unpredictably, a depletable budget is far more practical than a static permission table. The delegation model (parent partitions budget to child) is elegant and prevents privilege escalation by construction.

### 3. Fast/Slow Path Separation Is Pragmatically Smart

Most tool calls are safe and need zero friction. Only the dangerous ones need human intervention. This avoids the two failure modes of existing systems: either everything requires approval (unusable) or nothing does (unsafe).

### 4. Error-Feedback-to-LLM Loop (Castor Dam)

Catching `ValidationError` and converting it to natural language feedback instead of crashing is exactly what production agent systems need. This turns type errors into self-correction opportunities.

### 5. Two-Phase Roadmap (Python -> Rust) Is Pragmatic

Validating the state machine and API ergonomics in Python first, then pushing the hot path to Rust via PyO3, is a proven strategy (see: Pydantic V2, Ruff, uv). The FFI boundary at `import castor` is clean.

## Critical Questions and Gaps

### 1. Coroutine Serialization — The Elephant in the Room

The documents repeatedly mentioned "pickling/serializing the coroutine state," "serializing local variables and call stack," and `AgentProcess.serialize() -> bytes`. But in Python, you cannot pickle a live `asyncio` coroutine. Coroutine frames hold references to the event loop, closures, and C-level stack frames that are fundamentally non-serializable.

This was arguably the single hardest technical challenge in Castor, and it was a `pass` in the DDD.

**Resolution:** Adopted the checkpoint/replay model (Approach B). See the revised DDD Sections 2.3, 3.2–3.4, and 5–6.

### 2. "Preemptive" Is Actually Cooperative

The documents called Castor Stream a "preemptive" scheduler, but what was described is interruption at syscall boundaries (tool call points). True preemption means interrupting at arbitrary execution points — mid-computation, mid-thought.

In practice, the LLM can only be "interrupted" between inference calls (between tool calls or between generation steps). This is cooperative scheduling — the agent yields control at defined points (syscalls), and the kernel decides whether to proceed or suspend.

Open questions:
- Can a human abort a running LLM inference mid-stream? Or only before the next tool executes?
- If the LLM enters an infinite reasoning loop without making syscalls, is there a watchdog?
- What happens if a tool execution itself hangs?

### 3. Context Paging Is Lossy — Unlike Real Memory

Castor Lodge maps beautifully to an MMU conceptually, but with one fundamental difference: real memory paging is lossless; context summarization/vectorization is lossy. When you "page out" conversation history by summarizing it, you lose information irreversibly. When you "page in" via vector search, retrieval is probabilistic, not exact.

This creates tension with Castor's "deterministic kernel" philosophy. Open questions:
- What is the eviction policy? LRU? Priority-based? Agent-hinted?
- How do you handle the case where a "paged out" detail becomes critical later? (e.g., the agent needs an exact number from 50 messages ago)
- Has a tiered approach been considered: full messages in hot storage, compressed-but-lossless in warm storage, summaries only in cold?

### 4. Data Model Inconsistencies Across Documents

The three documents defined overlapping but contradictory models:

| Concept | ADD | DDD (original) |
|---|---|---|
| Capability | `CapabilityToken(id, resource_type, ...)` | `Capability(resource_type, ...)` — no `id` |
| Process state | `TaskState(serialized_locals: bytes)` | `AgentProcess(pending_interrupt)` |
| Syscall request | `SyscallRequest(process_id)` | `SyscallRequest(caller_pid)` |
| Status enums | `RUNNING, SUSPENDED, COMPLETED, FAILED` | `INIT, RUNNING, SUSPENDED, COMPLETED, TERMINATED` |

**Resolution:** Unified in the revised DDD Section 3. The DDD is now the canonical source of truth for data models.

### 5. IPC Was Underspecified

The original sub-agent spawning flow was a 6-step list ending with "append findings to parent's context_history." For real multi-agent workflows, the following were missing:

- Streaming results (child sends partial findings back to parent)
- Bidirectional communication (parent sends follow-up instructions to running child)
- Recursive spawning (child spawns its own sub-agents, capability cascading)
- Timeouts and failure (child hangs or loops — deadline or kill signal)
- Fan-out/fan-in (parent spawns N children in parallel, collects results)

**Resolution:** Partially addressed in the revised DDD Section 4 (synchronous spawn, HITL propagation, async fan-out/fan-in). Streaming and bidirectional IPC remain open for future design.

### 6. Missing Operational Concerns

Several production-critical topics were absent from all three documents:

- **Observability:** No mention of logging, tracing, or metrics. For debugging a suspended-then-resumed agent flow, distributed tracing (e.g., OpenTelemetry spans per syscall) would be invaluable.
- **Crash recovery:** If the kernel process itself dies mid-execution, how is state recovered? Is there a WAL (Write-Ahead Log)? Are state writes to SQLite transactional?
- **Tool execution timeouts:** What if a tool (e.g., a network call) takes 10 minutes? Is there a watchdog timer?
- **Multi-tenancy:** Is Castor designed for one agent tree per process, or can it multiplex independent sessions?
- **Testing:** How do you test the kernel? Mock tools? Simulated HITL flows? Deterministic replay of recorded logs?

### 7. The L4 Positioning

The PRD references "L4 secure microkernel." The real L4/seL4 family provides formally verified security guarantees and unforgeable capability tokens enforced at the hardware level. It should be made explicit whether there is an intention to provide any formal guarantees (e.g., "a child can never exceed its parent's budget" proven by construction), or whether L4 is purely an inspirational analogy. Either answer is valid.

### 8. Scope Boundary with Existing Frameworks

The PRD says "not another LangChain" but does not clarify the integration story. Open questions:
- Is Castor consumed as a library (`import castor`) by an existing agent framework (e.g., OpenClaw), or is it a standalone server that agents connect to over HTTP/gRPC?
- How does the kernel handle different tool-calling conventions across LLM providers (OpenAI function calling vs. Anthropic tool use vs. local models)?

### 9. The Rust Rewrite Trigger

Phase 2 proposes rewriting the core in Rust with PyO3 bindings. Open questions:
- What specific performance bottleneck or safety concern motivates Phase 2?
- Is it GIL contention under concurrent agents? Serialization throughput? Memory safety of the state store?
- The Python prototype needs extremely clear boundaries about what crosses the FFI boundary.
- State serialization in Rust + Python interop is non-trivial.

## Decisions Made

| Decision | Chosen Approach | Rationale |
|---|---|---|
| Coroutine serialization | Checkpoint/Replay (Approach B) | LLM agents are linear; replay is natural; determinism constraint is free |
| HITL with modification | Log as HITL_MODIFIED, let LLM re-plan | Preserves replay integrity; human uses natural language, not JSON |
| Canonical data models | DDD is the source of truth | ADD models are superseded by revised DDD Section 3 |
