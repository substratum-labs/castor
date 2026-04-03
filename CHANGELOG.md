# Changelog

## v0.5.1 (2026-04-01)

### Fixes

- **`budgets=` alias** — `Castor(budgets={"api": 50})` now works as an alias for `default_budgets=` so the Quick Start example is copy-pasteable.

## v0.5.0 (2026-03-31)

### Features

- **Speculative Execution** — `kernel.run(agent, speculative=True)` runs the agent without HITL interruptions. Destructive operations are flagged with `needs_review` at execution time for post-hoc review.
- **Execution Summary** — `kernel.scan(cp)` produces an `ExecutionSummary` with `total_steps`, `auto_verified`, `flagged_count`, and per-tool usage stats.
- **Time-Travel (Fork)** — `cp.fork(at_step=N)` creates a new checkpoint rewound to step N. Steps 1..N replay from cache (free), steps N+1.. re-execute.
- **Plain functions in `Castor(tools=)`** — No `@castor_tool` decorator needed. Pass raw callables and Castor auto-wraps them with `ToolMetadata.from_function()`.
- **`Castor(destructive=[...])`** — Mark tool names as destructive/HITL without decorators.
- **`Castor(llm=callable)`** — Auto-wrap an LLM callable as a tracked syscall with configurable `llm_cost` and `llm_resource`.
- **`Castor(roche=True)`** — One-line Roche sandbox integration. Requires `roche-sandbox[castor]` package.
- **`(name, func)` tuples** — `Castor(tools=[("search", my_search_fn)])` for explicit tool naming.
- **Enhanced CLI** — `castor run agent.py:main --tool tools.py:search --destructive --budget api=50 --speculative`.
- **`needs_review` flag** — Set at execution time on `SyscallRecord`, not post-hoc. Authoritative signal for review.
- **Journal protocol** — `JournalProtocol` and `InMemoryJournal` for swappable journal backends.
- **Protocol interfaces** — `GateProtocol`, `BudgetProtocol`, `CheckpointStoreProtocol`, `JournalProtocol` as component boundaries.

### Breaking Changes

- Destructive tools now auto-execute within budget by default, suspending only on budget exhaustion (not on every call).

## v0.4.0 (2026-03-08)

### Features

- **castor.lib patterns** — `parallel()`, `react()`, `map_reduce()`, `plan_execute()`, `conversation()`, `supervisor()` high-level agent patterns
- **`run_task()`** — Level-0 API: single function wrapping `react()` with auto-tool-discovery
- **Expanded CLI** — `castor run agent.py` with agent loading (`file:func` or convention fallback), `castor ps`, `castor inspect`, `castor approve/reject/modify`

## v0.3.0 (2026-03-08)

### Features

- **castor.lib** — Agent-facing standard library using `ContextVar[SyscallProxy]` bridge
  - Primitives: `tool()`, `chat()`, `budget()`, `try_tool()`, `spawn()`, `join()`
  - Agent code has zero kernel imports — clean operator/agent separation
- **Dual-signature detection** — `inspect.signature` auto-detects legacy (`proxy`) vs. new (no-arg) agent style
- **ContextVar set for all agents** — Enables gradual migration from proxy style to lib style

## v0.2.0 (2026-03-07)

### Breaking Changes

- **Naming overhaul** — All kernel modules renamed for clarity:
  - `dam/` → `gate/` (`CastorDam` → `SyscallGate`)
  - `stream/` → `scheduler/`
  - `lodge/` → `mmu/` (`CastorLodge` → `MMU`)
  - `kernel.dam` → `kernel.gate`, `Castor(dam=...)` → `Castor(gate=...)`

## v0.1.0 (2026-03-03)

Initial release — Python prototype of the Castor microkernel.

### Features

- **SyscallGate** — Tool registry with `@castor_tool` decorator, Pydantic V2 schema validation, natural language error feedback
- **Scheduler** — Checkpoint/replay execution model, SyscallProxy gateway, AgentRunner, preemption via `asyncio.Task.cancel()`
- **MMU** — Context window memory management with FIFO eviction, pinning, semantic memory driver HAL
- **Capability Manager** — Budget-tracked permissions with delegation, reclamation, and refund on failure
- **Human-in-the-Loop** — Destructive tool suspension, approve/reject/modify with replay-safe feedback
- **Sub-Agent Spawning** — Sync and async spawn/join with deterministic PIDs, budget isolation, child HITL propagation
- **Crash Recovery** — Write-ahead log for syscall execution with automatic budget refund on recovery
- **Observability** — Structured logging, optional OpenTelemetry tracing and Prometheus metrics
- **CLI** — `castor list`, `castor show`, `castor reject`, `castor modify` commands
- **API Stability** — `@stable` and `@experimental` markers on all public APIs

### Known Limitations

- MMU context paging is lossy (evicted messages are summarized, retrieval is probabilistic)
- No real `SemanticMemoryDriver` implementation (HAL interface only)
- CLI cannot approve HITL (requires runtime with Gate + CapabilityManager)
- No crash recovery for in-flight async child tasks
- No streaming or bidirectional IPC between agents
