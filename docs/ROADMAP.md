# Project Castor — Roadmap

## Phase 1: Python Prototype

The goal of Phase 1 is to validate the architecture, API ergonomics, and core execution model in Python. All four kernel subsystems are implemented, tested, and usable via `import castor`.

### Milestone 1: Core Kernel (Dam + Capability) — COMPLETE

The foundation — tool registration, validation, and budget management.

**Castor Dam (Tool Registry & Validation)** — `src/castor/dam/`
- [x] `@castor_tool` decorator: registers functions with Pydantic schemas (`decorator.py`)
- [x] Auto-generate input schema from function signature + type hints via `pydantic.create_model()`
- [x] Tool registry: register, lookup, list tools (`registry.py`, `ToolRegistry`)
- [x] Input validation via Pydantic: reject malformed LLM output (`validator.py`, `CastorDam.validate()`)
- [x] Error-to-feedback: catch `ValidationError`, return natural language `SyscallResponse` for LLM self-correction (`CastorDam.format_validation_error()`)
- [x] Tool metadata: `consumes`, `cost_per_use`, `destructive`, `requires_hitl` (`ToolMetadata`)

**Capability Manager (Budget Tracking)** — `src/castor/capability/`
- [x] Capability creation: initialize root budgets for an agent (`CapabilityManager.create_capabilities()`)
- [x] Budget deduction: deduct cost on tool execution (`CapabilityManager.deduct()`)
- [x] Budget check: reject syscall if insufficient budget (`CapabilityManager.check()`)
- [x] Capability delegation: parent partitions budget subset to child (`CapabilityManager.delegate()`)
- [x] Capability reclamation: return unused child budget to parent on completion (`CapabilityManager.reclaim()`)
- [x] `CapabilityExhaustedError`: signal when budget is depleted

### Milestone 2: Scheduler (Stream) — COMPLETE

The execution engine — checkpoint/replay, HITL, and preemption.

**SyscallProxy (The Replay Gateway)** — `src/castor/stream/proxy.py`
- [x] Replay path: serve cached responses from `syscall_log`
- [x] Replay assertion: detect divergence between expected and actual requests (`ReplayDivergenceError`)
- [x] Fast path: validate → deduct capability → execute → log
- [x] Slow path: set `pending_hitl`, raise `SuspendInterrupt`
- [x] Integration with Dam (validation) and Capability Manager (deduction)
- [x] Validation errors returned as `SyscallResponse` feedback (not exceptions)
- [x] Capability exhaustion returned as `INSUFFICIENT_CAPABILITY` feedback

**AgentRunner (Kernel Executor)** — `src/castor/stream/runner.py`
- [x] Run agent function as `asyncio.Task` (`run_as_task()`)
- [x] Handle three exit modes: completion, `SuspendInterrupt`, `CancelledError`
- [x] Preemption via `task.cancel()` with reason/payload (`preempt()`)
- [x] Checkpoint persistence to SQLite (`CheckpointStore.save()`)
- [x] Checkpoint loading from SQLite (`CheckpointStore.load()`)

**CheckpointStore (SQLite Persistence)** — `src/castor/stream/persistence.py`
- [x] Save/load `AgentCheckpoint` via `model_dump_json()` / `model_validate_json()`
- [x] SQLAlchemy ORM with upsert pattern
- [x] Nested checkpoint support (child_checkpoint round-trips correctly)
- [x] Delete and list operations

**HITL Feedback Loop** — `src/castor/stream/hitl.py`
- [x] Approve: execute pending syscall, log with `was_hitl=True`, replay
- [x] Reject: log with `HITL_REJECTED` response, replay (LLM re-plans)
- [x] Approve with modification: log as `HITL_MODIFIED` with human feedback, replay (LLM re-plans with feedback)

**Sub-Agent Spawning** — NOT YET IMPLEMENTED (data model ready)
- [ ] `spawn_agent` syscall: synchronous (blocking) child execution
- [ ] Capability delegation to child, reclamation on completion
- [ ] Child HITL propagation: child suspension bubbles to parent
- [ ] `spawn_agent_async` / `join_agent`: non-blocking fan-out/fan-in
- [x] Nested checkpoint storage: child checkpoint inside parent's `SyscallRecord` (model support complete)

### Milestone 3: Context Manager (Lodge) — NOT STARTED

Context window management — the memory pager. Architecturally independent; design discussion pending.

- [ ] Token counting for agent `context_history`
- [ ] Pinning: mark system instructions as non-evictable
- [ ] Paging threshold: detect when context approaches max tokens
- [ ] Eviction policy: select which messages to page out
- [ ] Page out: summarize/compress old messages, store in local DB
- [ ] Page in: retrieve relevant context via search when needed

### Milestone 4: Integration & Testing — PARTIALLY COMPLETE

- [x] End-to-end test: HITL suspension, approve, and resume via replay
- [x] Preemption test: cancel mid-execution, verify checkpoint consistency, resume
- [x] Replay determinism test: verify same syscall sequence on replay
- [ ] Capability delegation test: parent → child budget flow (requires sub-agent spawning)
- [x] Error feedback test: validation failure → LLM self-correction
- [x] HITL reject test: agent re-plans after rejection
- [x] HITL modify test: agent receives feedback and issues revised syscall
- [x] Capability exhaustion test: budget depletes mid-run
- [ ] CLI or simple API for human approval (webhook or interactive prompt)

**Test coverage:** 90 tests across 8 test files, 0 lint errors.

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

### Integration & Interoperability

Castor is a microkernel, not an agent framework. It provides core primitives (capability security, checkpoint/replay, HITL) that existing agent frameworks and custom agents integrate with.

- Adapter layer for popular agent frameworks (LangChain, CrewAI, AutoGen) to route tool calls through `SyscallProxy`
- Provider-agnostic design: agent functions bring their own LLM client — Castor never touches inference
- Example integrations showing how to wrap an existing agent loop with Castor's kernel
- `pip install castor` with zero mandatory dependencies beyond Pydantic + SQLAlchemy

### Debugging & Introspection

Utilities for kernel operators and framework integrators — not end-user CLI tooling.

- `castor.replay(checkpoint)`: programmatic replay of a checkpoint for debugging
- `castor.inspect(checkpoint)`: dump syscall log and capability state
- Documentation for framework authors: how to integrate checkpoint/replay into your agent loop
- Example agents demonstrating HITL, preemption, and sub-agent patterns
