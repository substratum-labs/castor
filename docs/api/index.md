# API Reference

Complete reference for the Castor public API.

## Stability Levels

Castor marks each export with a stability level:

- **Stable** -- Safe for production use. Breaking changes follow semver.
- **Experimental** -- API may change between minor versions.

## Stable API

| Class / Function | Module | Purpose |
|------------------|--------|---------|
| [`Castor`](kernel.md) | `castor.core` | Unified kernel facade |
| [`SyscallProxy`](proxy.md) | `castor.scheduler.proxy` | Agent-side syscall gateway |
| [`AgentCheckpoint`](models.md#castor.models.checkpoint.AgentCheckpoint) | `castor.models.checkpoint` | Serializable agent state |
| [`SyscallRecord`](models.md#castor.models.checkpoint.SyscallRecord) | `castor.models.checkpoint` | Single syscall request/response |
| [`CastorMessage`](models.md#castor.models.checkpoint.CastorMessage) | `castor.models.checkpoint` | Context window message |
| [`SuspendInterrupt`](hitl.md#castor.models.checkpoint.SuspendInterrupt) | `castor.models.checkpoint` | HITL suspension signal |
| [`Capability`](models.md#castor.models.capability.Capability) | `castor.models.capability` | Budget tracking per resource |
| [`SyscallRequest`](models.md#castor.models.capability.SyscallRequest) | `castor.models.capability` | Validated tool request |
| [`SyscallResponse`](models.md#castor.models.capability.SyscallResponse) | `castor.models.capability` | Tool execution response |
| [`SyscallResult`](models.md#castor.models.result.SyscallResult) | `castor.models.result` | Structured HITL-aware result |
| [`SyscallGate`](gate.md#castor.gate.validator.SyscallGate) | `castor.gate.validator` | Tool validation & execution |
| [`castor_tool`](gate.md#castor.gate.decorator.castor_tool) | `castor.gate.decorator` | Tool registration decorator |
| [`CapabilityManager`](capability.md) | `castor.capability.manager` | Budget creation & tracking |
| [`HITLHandler`](hitl.md#castor.scheduler.hitl.HITLHandler) | `castor.scheduler.hitl` | Approve / reject / modify |
| [`AgentRunner`](runner.md#castor.scheduler.runner.AgentRunner) | `castor.scheduler.runner` | Agent execution loop |
| [`CheckpointStore`](runner.md#castor.scheduler.persistence.CheckpointStore) | `castor.scheduler.persistence` | SQLite persistence |
| [`ExecutionSummary`](kernel.md#castor.kernel.summary.ExecutionSummary) | `castor.kernel.summary` | Speculative execution review summary |
| [`InMemoryJournal`](kernel.md) | `castor.kernel.journal` | Dict-backed journal for replay |
| `auto_approve` | `castor.hitl_policies` | Auto-approve HITL policy |
| `auto_reject` | `castor.hitl_policies` | Auto-reject HITL policy |
| `interactive` | `castor.hitl_policies` | Terminal interactive policy |

## Agent Developer API (`castor.lib`)

| Function | Module | Purpose |
|----------|--------|---------|
| [`tool()`](lib.md) | `castor.lib.primitives` | Call a registered tool by name |
| [`chat()`](lib.md) | `castor.lib.primitives` | Call an LLM tool |
| [`budget()`](lib.md) | `castor.lib.primitives` | Check remaining budget |
| [`try_tool()`](lib.md) | `castor.lib.primitives` | Call a tool (failure-expected semantic) |
| [`spawn()`](lib.md) | `castor.lib.spawn` | Spawn a child agent |
| [`join()`](lib.md) | `castor.lib.spawn` | Wait for child agent result |
| [`parallel()`](lib.md) | `castor.lib.patterns` | Execute multiple tool calls |
| [`react()`](lib.md) | `castor.lib.patterns` | ReAct loop (Think → Act → Observe) |
| [`map_reduce()`](lib.md) | `castor.lib.patterns` | Map items through tools, then reduce |
| [`plan_execute()`](lib.md) | `castor.lib.patterns` | LLM plans steps, then executes |
| [`conversation()`](lib.md) | `castor.lib.patterns` | Multi-turn chat loop |
| [`supervisor()`](lib.md) | `castor.lib.patterns` | LLM delegates to sub-agents |
| [`run_task()`](lib.md) | `castor.lib.run_task` | Level 0: one-sentence goal → result |

## CLI

| Command | Purpose |
|---------|---------|
| [`castor run`](cli.md) | Run an agent from a Python file (`--speculative`, `--tool`, `--destructive`) |
| [`castor ps`](cli.md) | List all agent checkpoints |
| [`castor inspect`](cli.md) | Show checkpoint details |
| [`castor approve`](cli.md) | Approve a pending HITL request |
| [`castor reject`](cli.md) | Reject a pending HITL request |
| [`castor modify`](cli.md) | Modify a pending HITL request |

## Experimental API

| Class / Function | Module | Purpose |
|------------------|--------|---------|
| [`CastorTask`](kernel.md#castor.core.CastorTask) | `castor.core` | Background task with preemption |
| [`LLMSyscall`](llm.md#castor.llm.wrapper.LLMSyscall) | `castor.llm.wrapper` | Non-streaming LLM wrapper |
| [`StreamingLLMSyscall`](llm.md#castor.llm.wrapper.StreamingLLMSyscall) | `castor.llm.wrapper` | Streaming LLM with preemption |
| [`MMU`](mmu.md) | `castor.mmu.core` | Context window management |
| [`AgentRegistry`](agents.md) | `castor.scheduler.agent_registry` | Sub-agent registry |
| [`castor_agent`](agents.md#castor.scheduler.agent_registry.castor_agent) | `castor.scheduler.agent_registry` | Agent registration decorator |
