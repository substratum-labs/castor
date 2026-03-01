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
- [x] Transactional budget: `deduct` → `execute` → `refund()` on failure (prevents budget leaks)
- [x] LLM replay safety: `LLMSyscall` wrapper routes inference through `proxy.syscall()` (`castor.llm.wrapper`)
- [x] Kernel-internal replay skip: `kernel_tool_names` auto-consumed during replay for Lodge page-out records

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

**Sub-Agent Spawning** — `src/castor/stream/agent_registry.py`, `proxy.py`, `hitl.py`
- [x] `AgentRegistry`: register/lookup agent functions by name, `@castor_agent` decorator
- [x] `spawn_agent` syscall: synchronous (blocking) child execution via `SyscallProxy._handle_spawn()`
- [x] Capability delegation to child, reclamation on completion
- [x] Deterministic child PID: `parent_pid::agent_name-N`
- [x] Child HITL propagation: child suspension bubbles to parent (`_propagate_child_suspension`)
- [x] Child HITL resolution: `approve_child_hitl`, `reject_child_hitl`, `modify_child_hitl` on `HITLHandler`
- [x] `AgentRunner` wired with `AgentRegistry` for spawn support
- [x] Nested checkpoint storage: child checkpoint inside parent's `SyscallRecord`
- [ ] `spawn_agent_async` / `join_agent`: non-blocking fan-out/fan-in

### Milestone 3: Context Manager (Lodge) — COMPLETE

Context window memory management — the agentic MMU. Monitors token usage, evicts unpinned messages to cold storage, and provides page-in search.

**CastorLodge (MMU Controller)** — `src/castor/lodge/core.py`
- [x] Token counting: sum `CastorMessage.token_count` fields with fallback to `TokenCounter` estimation
- [x] Pinning: `CastorMessage.pinned=True` messages are never evicted (system prompts, HITL records)
- [x] Watermark threshold: configurable high-water mark triggers eviction
- [x] FIFO eviction: select oldest unpinned messages until total tokens <= watermark
- [x] Eviction via syscall: `sys_kernel_page_out` routed through `proxy.syscall()` for replay safety
- [x] Kernel-internal replay skip: `kernel_tool_names` auto-consumed during replay (side-effects already applied)
- [x] Eviction hook: fires in `SyscallProxy` before LLM tool calls (live execution only)
- [x] `search_memory` tool: agent-facing page-in via `proxy.syscall()`, replay-safe

**SemanticMemoryDriver (HAL)** — `src/castor/lodge/driver.py`
- [x] Abstract base class: `ingest()` and `search()` methods
- [x] Strategy/mechanism separation: Lodge core never imports concrete drivers

**TokenCounter Protocol** — `src/castor/lodge/token_counter.py`
- [x] `TokenCounter` protocol: pluggable tokenizer interface
- [x] `CharCountEstimator`: default `len(text) // 4` estimator (no tiktoken dependency)

**InMemoryDriver (Mock)** — `src/castor/lodge/drivers/mock_driver.py`
- [x] Dict-based storage with substring search for testing

**CastorMessage** — `src/castor/models/checkpoint.py`
- [x] Pydantic model with `role`, `content`, `pinned`, `token_count` fields
- [x] `context_history` upgraded to `list[CastorMessage | dict[str, Any]]`

### Milestone 4: Integration & Testing — PARTIALLY COMPLETE

- [x] End-to-end test: HITL suspension, approve, and resume via replay
- [x] Preemption test: cancel mid-execution, verify checkpoint consistency, resume
- [x] Replay determinism test: verify same syscall sequence on replay
- [x] Capability delegation test: parent → child budget flow
- [x] Error feedback test: validation failure → LLM self-correction
- [x] HITL reject test: agent re-plans after rejection
- [x] HITL modify test: agent receives feedback and issues revised syscall
- [x] Capability exhaustion test: budget depletes mid-run
- [x] Lodge eviction + page-in integration test: large context triggers eviction, `search_memory` retrieves
- [x] Lodge pinned survival test: pinned messages survive extreme eviction
- [x] Lodge replay determinism test: `driver.ingest` not called on replay after HITL approve
- [ ] CLI or simple API for human approval (webhook or interactive prompt)

**Test coverage:** 142 tests across 11 test files, 0 lint errors.

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
