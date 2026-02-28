# Project Castor — Roadmap

## Phase 1: Python Prototype

The goal of Phase 1 is to validate the architecture, API ergonomics, and core execution model in Python. All four kernel subsystems are implemented, tested, and usable via `import castor`.

### Milestone 1: Core Kernel (Dam + Capability)

The foundation — tool registration, validation, and budget management.

**Castor Dam (Tool Registry & Validation)**
- [ ] `@castor_tool` decorator: registers functions with Pydantic schemas
- [ ] Auto-generate input schema from function signature + type hints
- [ ] Tool registry: register, lookup, list tools
- [ ] Input validation via Pydantic: reject malformed LLM output
- [ ] Error-to-feedback: catch `ValidationError`, return natural language `SyscallResponse` for LLM self-correction
- [ ] Tool metadata: `consumes`, `cost_per_use`, `destructive`, `requires_hitl`

**Capability Manager (Budget Tracking)**
- [ ] Capability creation: initialize root budgets for an agent
- [ ] Budget deduction: deduct cost on tool execution
- [ ] Budget check: reject syscall if insufficient budget
- [ ] Capability delegation: parent partitions budget subset to child
- [ ] Capability reclamation: return unused child budget to parent on completion
- [ ] `CapabilityExhaustedInterrupt`: signal when budget is depleted

### Milestone 2: Scheduler (Stream)

The execution engine — checkpoint/replay, HITL, and preemption.

**SyscallProxy (The Replay Gateway)**
- [ ] Replay path: serve cached responses from `syscall_log`
- [ ] Replay assertion: detect divergence between expected and actual requests
- [ ] Fast path: validate → deduct capability → execute → log
- [ ] Slow path: set `pending_hitl`, raise `SuspendInterrupt`
- [ ] Integration with Dam (validation) and Capability Manager (deduction)

**AgentRunner (Kernel Executor)**
- [ ] Run agent function as `asyncio.Task`
- [ ] Handle three exit modes: completion, `SuspendInterrupt`, `CancelledError`
- [ ] Preemption via `task.cancel()` with reason/payload
- [ ] Checkpoint persistence to SQLite (serialize `AgentCheckpoint`)
- [ ] Checkpoint loading from SQLite (deserialize and resume)

**HITL Feedback Loop**
- [ ] Approve: execute pending syscall, log with `was_hitl=True`, replay
- [ ] Reject: log with `HITL_REJECTED` response, replay (LLM re-plans)
- [ ] Approve with modification: log as `HITL_MODIFIED` with human feedback, replay (LLM re-plans with feedback)

**Sub-Agent Spawning**
- [ ] `spawn_agent` syscall: synchronous (blocking) child execution
- [ ] Capability delegation to child, reclamation on completion
- [ ] Child HITL propagation: child suspension bubbles to parent
- [ ] `spawn_agent_async` / `join_agent`: non-blocking fan-out/fan-in
- [ ] Nested checkpoint storage: child checkpoint inside parent's `SyscallRecord`

### Milestone 3: Context Manager (Lodge)

Context window management — the memory pager.

- [ ] Token counting for agent `context_history`
- [ ] Pinning: mark system instructions as non-evictable
- [ ] Paging threshold: detect when context approaches max tokens
- [ ] Eviction policy: select which messages to page out
- [ ] Page out: summarize/compress old messages, store in local DB
- [ ] Page in: retrieve relevant context via search when needed

### Milestone 4: Integration & Testing

- [ ] End-to-end test: multi-agent workflow with HITL suspension and resume
- [ ] Preemption test: cancel mid-execution, verify checkpoint consistency, resume
- [ ] Replay determinism test: verify same syscall sequence on replay
- [ ] Capability delegation test: parent → child budget flow
- [ ] Error feedback test: validation failure → LLM self-correction
- [ ] CLI or simple API for human approval (webhook or interactive prompt)

---

## Phase 2: Rust Performance Core (via PyO3)

Rewrite performance-critical kernel internals in Rust, exposed to Python via PyO3/Maturin. Upper-layer developers still use `import castor` with Python APIs.

### Targets for Rust Rewrite

- [ ] Checkpoint serialization/deserialization (replace Pydantic JSON with zero-copy Rust serde)
- [ ] Capability budget tracking (lock-free atomic operations for concurrent agents)
- [ ] Tool validation (Rust-native schema validation for hot path)
- [ ] SQLite state store (Rust-native SQLite bindings for reduced overhead)

### FFI Boundary Design

- [ ] Define which types cross the Python ↔ Rust boundary
- [ ] PyO3 wrapper for `AgentCheckpoint`, `SyscallRecord`, `Capability`
- [ ] Python-side API unchanged: `from castor import SyscallProxy, castor_tool`
- [ ] Benchmark: measure serialization, validation, and scheduling overhead before/after

---

## Future Features (Post-Phase 2)

Ideas discussed but not yet designed. Each would require its own design document.

### Observability & Debugging

- OpenTelemetry spans per syscall (distributed tracing across agent trees)
- Structured logging for kernel events (spawn, suspend, resume, preempt, complete)
- Metrics: syscall latency, tool execution time, replay cost, capability utilization
- Replay debugger: load a checkpoint and step through the syscall log interactively

### Crash Recovery

- Write-ahead log (WAL) for checkpoint persistence
- Transactional SQLite writes (ensure checkpoint is never partially written)
- Recovery on kernel restart: detect in-progress agents, resume from last persisted checkpoint
- Idempotency keys for tools to prevent double-execution after crash recovery

### Advanced IPC

- Streaming results: child sends partial findings back to parent during execution
- Bidirectional communication: parent sends follow-up instructions to a running child
- Recursive spawning: child spawns its own sub-agents with capability cascading
- Agent groups: coordinate N agents with shared state or consensus protocols

### Multi-Tenancy

- Session isolation: multiple independent agent trees in a single Castor process
- Per-session capability budgets and resource limits
- Session scheduling: priority queues across sessions
- Session persistence: each session has its own checkpoint store

### Context Paging (Advanced)

- Tiered storage: full messages (hot) → compressed-but-lossless (warm) → summaries (cold)
- Agent-hinted eviction: agent marks messages as "keep" or "safe to summarize"
- Retrieval-augmented paging: vector search for page-in, with relevance scoring
- Lossless compression for structured data (JSON, tables) vs. lossy for narrative

### Formal Guarantees

- Prove by construction: "a child can never exceed its parent's budget"
- Prove: "a destructive tool never executes without HITL approval"
- Property-based testing with Hypothesis for invariant validation
- Move from "inspired by L4" to "verified like seL4" for critical properties

### LLM Provider Abstraction

- Unified tool-calling interface across providers (OpenAI, Anthropic, local models)
- Provider-specific streaming adapters
- Model-agnostic agent functions (no provider lock-in)

### SDK & Developer Experience

- `castor init`: scaffold a new agent project
- `castor run`: execute an agent with interactive HITL prompts
- `castor replay`: replay a checkpoint for debugging
- `castor inspect`: view an agent's syscall log and capability state
- Documentation, tutorials, and example agents
