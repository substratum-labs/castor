# Design Review: Project Castor

> **Version:** 2.0 — Post-implementation review (Phase 1 complete)
> **Scope:** Full review of implemented system against original PRD, ADD, DDD.

---

## Part I: Pre-Implementation Review (Historical)

This section preserves the original architectural review conducted before
implementation. Issues marked "RESOLVED" were addressed during development.

### Strengths Identified

#### 1. The OS Metaphor Maps Well

| OS Concept | Castor Analog | Why it fits |
|---|---|---|
| User/Kernel space | LLM vs. Engine | The trust boundary is real, not metaphorical |
| Syscalls | Tool invocations | LLM can only act through a validated gate |
| Capabilities | Budget tokens | Resource control that degrades gracefully |
| Process scheduling | Agent lifecycle | Suspend/resume maps to HITL naturally |
| Virtual memory/paging | Context window mgmt | Finite "memory" with eviction strategies |

#### 2. Capability-Based Security Is the Right Call

ACLs say "who can do what." Capabilities say "how much can you still do." For
agents that burn through resources unpredictably, a depletable budget is far
more practical. Delegation prevents privilege escalation by construction.

#### 3. Fast/Slow Path Separation

Most tool calls are safe and need zero friction. Only dangerous ones need human
intervention. Avoids the two failure modes: everything requires approval
(unusable) or nothing does (unsafe).

#### 4. Error-Feedback-to-LLM Loop

Catching `ValidationError` and converting to natural language feedback instead
of crashing is exactly what production agent systems need.

#### 5. Two-Phase Roadmap (Python -> Rust)

Validating in Python first, then pushing to Rust via PyO3 is proven strategy
(Pydantic V2, Ruff, uv).

### Pre-Implementation Questions and Resolutions

| Question | Resolution | Status |
|---|---|---|
| Coroutine serialization (can't pickle asyncio) | Checkpoint/Replay model adopted | RESOLVED |
| "Preemptive" is actually cooperative | `asyncio.Task.cancel()` gives true preemption at `await` points | RESOLVED |
| Context paging is lossy (unlike real memory) | FIFO eviction with pinning; acknowledged as design trade-off | RESOLVED |
| Data model inconsistencies across docs | DDD is canonical source of truth | RESOLVED |
| IPC was underspecified | Sync spawn, async spawn/join, child HITL propagation implemented | PARTIALLY RESOLVED |
| Missing observability | Not yet addressed | OPEN |
| Crash recovery | Not yet addressed | OPEN |
| Tool execution timeouts | Preemption via `task.cancel()` covers async tools | PARTIALLY RESOLVED |
| Multi-tenancy | Single agent tree per process | OPEN |
| L4 positioning | Inspirational analogy, not formal verification | CLARIFIED |
| Scope boundary with existing frameworks | Library via `import castor` | CLARIFIED |
| Rust rewrite trigger | Dam validation as hot path candidate | OPEN |

---

## Part II: Post-Implementation Review

### 1. Implementation Completeness vs. PRD

| PRD Requirement | Status | Notes |
|---|---|---|
| Strongly-typed tool sandbox (Dam) | COMPLETE | Pydantic V2, auto-schema, self-correction feedback |
| Capability-driven security | COMPLETE | Token-bucket, atomic delegation, refund-on-failure |
| Fast/Slow path separation | COMPLETE | HITL gate in SyscallProxy step 6 |
| Checkpoint/Replay scheduler | COMPLETE | Deterministic replay, 9-step pipeline |
| HITL feedback loop (approve/reject/modify) | COMPLETE | Including child HITL propagation |
| Context window paging (Lodge) | COMPLETE | FIFO eviction, watermark, pinning, search_memory |
| Sub-agent spawning | COMPLETE | Sync + async fan-out/fan-in |
| Preemptive scheduling | COMPLETE | `task.cancel()` + checkpoint consistency |
| CLI for checkpoint inspection | COMPLETE | list, show, reject, modify with safety guards |
| SQLite persistence | COMPLETE | Via SQLAlchemy, upsert semantics |

**All core PRD requirements are implemented.** No feature gaps in Phase 1 scope.

### 2. Architecture Quality Assessment

#### 2.1 Separation of Concerns: EXCELLENT

Each subsystem has a single responsibility and clear boundaries:
- Dam: validation and execution only
- Stream: scheduling and replay only
- Lodge: memory management only
- Capability: budget tracking only
- LLM wrapper: replay-safe inference only

Subsystems communicate through well-defined interfaces. No circular runtime
dependencies (Lodge/AgentRegistry use `TYPE_CHECKING` imports).

#### 2.2 Replay Determinism: EXCELLENT

The 9-step syscall pipeline enforces deterministic replay rigorously:
- Kernel tool skip during replay
- Request matching with `ReplayDivergenceError` on mismatch
- HITL modification preserves original request in log
- Spawn/join intercepts produce reproducible child PIDs
- Lodge eviction hook gated by `not is_replaying`

The `_replay_index` / `_append_record` invariant ensures new syscalls always
advance the cursor past all logged records.

#### 2.3 Budget Safety: EXCELLENT

Three-layer budget protection:
1. **Deduct-before-execute** with refund-on-exception
2. **Atomic delegation** (validate-all-then-commit)
3. **Async spawn budget guard** (try/except reclaim after delegation)

No known budget leak scenarios in the current implementation.

#### 2.4 Error Handling: VERY GOOD

- Validation errors returned as structured responses (LLM self-corrects)
- Capability exhaustion returned as responses (not exceptions)
- Tool execution failures refunded and re-raised
- Spawn failures reclaim delegated budget

**Minor gap:** No structured error for `ToolNotFoundError` — it raises as a
Python exception rather than an `SyscallResponse`. This is acceptable as it
represents a programming error (not LLM error), but could be improved.

#### 2.5 Testability: EXCELLENT

- 170 tests across 14 files with clear module-to-test mapping
- No external service dependencies
- In-memory SQLite for persistence tests
- `InMemoryDriver` for Lodge tests
- Mock LLM callables for replay tests
- All tests self-contained and fast (~14 seconds)

#### 2.6 Code Quality: VERY GOOD

- 0 ruff lint errors
- Consistent use of Pydantic V2 patterns
- Python 3.11+ builtins throughout
- Clear docstrings on public APIs
- Defensive annotation access pattern shared across modules

### 3. Identified Risks and Concerns

#### 3.1 MEDIUM: Context Paging is Lossy

Unlike real memory paging, evicted context is summarized/vectorized and
retrieved probabilistically. If the LLM needs an exact detail from an evicted
message, `search_memory` may not retrieve it. The current `InMemoryDriver`
uses substring search (testing only); production drivers need careful
evaluation of retrieval quality.

**Recommendation:** Consider tiered storage: full messages in warm storage,
compressed/summarized in cold storage. Add metrics for retrieval precision.

#### 3.2 MEDIUM: No Crash Recovery

If the kernel process dies between a tool execution and `_append_record()`,
the budget is deducted but the result is lost. On restart, the checkpoint
is stale (pre-tool-execution state), and replaying will re-execute and
re-deduct.

**Recommendation:** Consider write-ahead logging: log the deduction intent
before execution, commit the result after. Or accept the risk with
documentation noting that tool executions should be idempotent.

#### 3.3 MEDIUM: Async Spawn Observability Gap

Child checkpoints from `spawn_agent_async` are not persisted until
`join_agent` completes. If the parent is preempted between spawn and join,
child tasks are orphaned with no trace in SQLite. The parent checkpoint has
the spawn record (with child PID as response) but no child checkpoint.

**Recommendation:** Consider persisting child checkpoints at spawn time
(not just join time) for observability. Add a GC mechanism for orphaned
child checkpoints.

#### 3.4 LOW: Singleton Default Registry

`dam.registry.default_registry` is a module-level singleton. Multiple test
files or concurrent kernel instances could share state if they accidentally
use the default. Current tests avoid this by creating fresh registries.

**Recommendation:** Document that production use should always create explicit
`ToolRegistry` instances, not rely on `default_registry`.

#### 3.5 LOW: No Tool Execution Timeout

While preemption via `task.cancel()` works for async tools (which have
`await` points), a purely CPU-bound tool with no `await` points would block
the event loop indefinitely and resist cancellation.

**Recommendation:** For CPU-bound tools, execute in `ProcessPoolExecutor`
with a kill timer. Document this constraint for tool authors.

#### 3.6 LOW: CLI Approve Gap

The CLI cannot approve HITL requests because approval requires Dam +
CapabilityManager runtime (not just SQLite). This is by design but means
the CLI covers 3 of 4 HITL actions.

**Recommendation:** This is acceptable. Document that a "resume server"
pattern is needed for production HITL approval.

### 4. Code-Level Observations

#### 4.1 SyscallProxy Complexity

`proxy.py` is the most complex file (~397 lines) with the 9-step pipeline,
spawn/join handlers, and propagation logic. This is architecturally correct
(it IS the kernel's central gateway) but any changes here carry high risk.

**Observation:** Well-structured with clear step comments. The internal
methods (`_handle_spawn`, `_handle_spawn_async`, `_handle_join`,
`_propagate_child_suspension`, `_append_record`) keep the main `syscall()`
method readable. No refactoring needed at this stage.

#### 4.2 HITLHandler Child Resume Pattern

`_resume_child()` creates a fresh `SyscallProxy` and replays the child.
This is correct but duplicates some logic from `AgentRunner.run()`. If
the runner's behavior changes (e.g., adding hooks), the child resume
path might diverge.

**Recommendation:** Consider having `_resume_child` delegate to
`AgentRunner.run()` to avoid behavioral divergence. This is a minor
risk at the current scale.

#### 4.3 Pydantic Model Building Duplication

`_generate_schema()` in `decorator.py` and `_build_input_model()` in
`validator.py` share nearly identical logic for introspecting function
signatures and building Pydantic models. This is a minor DRY violation.

**Observation:** The duplication is intentional — `_generate_schema`
produces a JSON Schema dict (for metadata), while `_build_input_model`
produces a model class (for validation). Unifying them would add coupling
between the decorator and validator modules.

### 5. Comparison with Existing Agent Frameworks

| Feature | Castor | LangChain | CrewAI | Temporal |
|---|---|---|---|---|
| Deterministic replay | Yes (syscall log) | No | No | Yes (event sourcing) |
| Budget management | Yes (capabilities) | No | No | No |
| HITL with modification | Yes (structured) | Manual | Manual | Yes (signals) |
| Context window management | Yes (Lodge) | Token counting | Token counting | N/A |
| Sub-agent spawning | Yes (sync + async) | Chains | Agent delegation | Child workflows |
| Type-safe tool validation | Yes (Pydantic) | Yes (Pydantic) | Partial | Schema-based |
| Preemptive scheduling | Yes (cancel) | No | No | Yes (cancel) |

**Castor's unique contribution** is combining deterministic replay with
capability-based security AND context window management in a single coherent
kernel. No existing framework provides all three.

---

## Part III: Recommendations for Phase 2

### Priority 1: Production Readiness

1. **Real SemanticMemoryDriver** — Implement a production driver with vector
   search (Qdrant, Pinecone, or local embeddings). Benchmark retrieval quality.
2. **Observability** — Add structured logging and optional OpenTelemetry spans
   for each syscall. Trace ID propagation through child agents.
3. **Crash recovery strategy** — Document idempotency requirements for tools,
   or implement write-ahead logging.

### Priority 2: Architecture Hardening

4. **Tool execution timeouts** — `ProcessPoolExecutor` for CPU-bound tools
   with configurable deadline.
5. **Async spawn checkpoint persistence** — Persist child checkpoints at
   spawn time for observability.
6. **Agent metrics** — Syscall count, budget utilization, eviction frequency,
   replay time per agent.

### Priority 3: Rust/PyO3 Core (Phase 2)

7. **FFI boundary analysis** — Profile the Python prototype to identify actual
   hot paths. Dam validation (Pydantic model building) is the likely candidate.
8. **State serialization** — Design the Rust <-> Python serialization boundary
   for `AgentCheckpoint`. Consider keeping Pydantic models in Python with a
   Rust core for the replay engine.
9. **Concurrency model** — Evaluate Tokio vs. asyncio interop via PyO3.

---

## Decisions Made During Implementation

| Decision | Chosen Approach | Rationale |
|---|---|---|
| Coroutine serialization | Checkpoint/Replay | LLM agents are linear; replay is natural; determinism is free |
| HITL with modification | Log as HITL_MODIFIED, let LLM re-plan | Preserves replay integrity; human uses natural language |
| Canonical data models | DDD is the source of truth | ADD models superseded |
| Preemption mechanism | `asyncio.Task.cancel()` + checkpoint/replay | True preemption with zero agent complexity |
| Preemption context | Metadata on checkpoint (not in syscall_log) | Agent-aware resume without breaking determinism |
| Lodge eviction policy | FIFO with watermark | Simple, predictable; upgrade to priority-based later |
| Lodge replay safety | Eviction through proxy, kernel tool skip | Lodge never checks `is_replaying` |
| Child HITL propagation | Nested checkpoints in parent's syscall record | Full state preserved for child resume |
| Async spawn handle | Child PID as handle | Deterministic, serializable, unique |
| Budget reclaim timing | At join (not at child completion) | Parent can observe child state at join |
