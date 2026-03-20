# Changelog

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
