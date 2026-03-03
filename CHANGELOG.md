# Changelog

## v0.1.0 (2026-03-03)

Initial release — Python prototype of the Castor microkernel.

### Features

- **Castor Dam** — Tool registry with `@castor_tool` decorator, Pydantic V2 schema validation, natural language error feedback
- **Castor Stream** — Checkpoint/replay execution model, SyscallProxy gateway, AgentRunner, preemption via `asyncio.Task.cancel()`
- **Castor Lodge** — Context window memory management with FIFO eviction, pinning, semantic memory driver HAL
- **Capability Manager** — Budget-tracked permissions with delegation, reclamation, and refund on failure
- **Human-in-the-Loop** — Destructive tool suspension, approve/reject/modify with replay-safe feedback
- **Sub-Agent Spawning** — Sync and async spawn/join with deterministic PIDs, budget isolation, child HITL propagation
- **Crash Recovery** — Write-ahead log for syscall execution with automatic budget refund on recovery
- **Observability** — Structured logging, optional OpenTelemetry tracing and Prometheus metrics
- **CLI** — `castor list`, `castor show`, `castor reject`, `castor modify` commands
- **API Stability** — `@stable` and `@experimental` markers on all public APIs

### Known Limitations

- Lodge context paging is lossy (evicted messages are summarized, retrieval is probabilistic)
- No real `SemanticMemoryDriver` implementation (HAL interface only)
- CLI cannot approve HITL (requires runtime with Dam + CapabilityManager)
- No crash recovery for in-flight async child tasks
- No streaming or bidirectional IPC between agents
