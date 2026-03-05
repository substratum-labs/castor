# smolagents Deep Integration (Level 2) — Design

**Date:** 2026-03-05
**Status:** APPROVED
**Prerequisite:** Level 1 guard layer (`examples/smolagents_guard/guard.py`) — COMPLETE
**Goal:** Add checkpoint/replay and HITL suspend/resume to smolagents via Castor's native data models, demonstrating the progressive integration story.

---

## 1. Progressive Integration Story

| Level | File | User Learns | Castor Concepts | User Code Change |
|-------|------|-------------|-----------------|-----------------|
| L1 | `guard.py` | "One swap adds budget + HITL" | `CapabilityManager` | +2 lines |
| L2 | `deep_guard.py` | "Crash recovery + suspend/resume" | `SyscallRecord`, `AgentCheckpoint`, `CheckpointStore` | +1 line (`checkpoint_store=`) |

```python
# Level 1:
agent = CastorGuardedAgent(tools=TOOLS, model=model, budgets=..., tool_policies=...)

# Level 2 — one parameter adds checkpoint/replay:
agent = CastorResilientAgent(tools=TOOLS, model=model, budgets=..., tool_policies=...,
                             checkpoint_store=CheckpointStore("sqlite:///agent.db"))
```

---

## 2. Architecture

Two hook points, one shared replay log:

```
CastorResilientAgent(ToolCallingAgent)
│
├── __init__(model=real_model, checkpoint_store=store, ...)
│   ├── wraps model with ReplayModel(real_model, agent)
│   └── loads/creates AgentCheckpoint
│
├── ReplayModel (wraps smolagents Model)
│   └── generate(messages, **kwargs)
│       ├── replaying? → return syscall_log[i].response (cached)
│       └── live? → call inner.generate() → record to syscall_log
│
├── execute_tool_call(tool_name, arguments)
│   ├── replaying? → return syscall_log[i].response (cached)
│   └── live? → budget → HITL gate → super() → record to syscall_log
│
├── _checkpoint: AgentCheckpoint  ← Castor's native model
│   └── syscall_log: list[SyscallRecord]
│
└── _store: CheckpointStore  ← SQLite persistence
```

LLM calls and tool calls are interleaved in the same `syscall_log`, preserving strict ordering — identical to Castor's `SyscallProxy` design.

---

## 3. Replay Mechanism

Core replay logic shared between both hooks:

```python
@property
def is_replaying(self) -> bool:
    return self._replay_index < len(self._checkpoint.syscall_log)

def _advance_replay(self, request: dict) -> Any:
    """Serve cached response. Raises ReplayDivergenceError on mismatch."""
    record = self._checkpoint.syscall_log[self._replay_index]
    if record.request != request:
        raise ReplayDivergenceError(self._replay_index, record.request, request)
    self._replay_index += 1
    return record.response

def _record(self, request: dict, response: Any) -> None:
    """Append a new syscall record and persist checkpoint."""
    self._checkpoint.syscall_log.append(
        SyscallRecord(request=request, response=response)
    )
    self._save_checkpoint()
```

### ReplayModel

```python
class ReplayModel(Model):
    def __init__(self, inner: Model, agent: CastorResilientAgent):
        super().__init__(model_id=f"castor-replay:{inner.model_id}")
        self._inner = inner
        self._agent = agent

    def generate(self, messages, **kwargs):
        request = {"tool_name": "__llm__", "arguments": {"n_messages": len(messages)}}

        if self._agent.is_replaying:
            return _deserialize_chat_message(self._agent._advance_replay(request))

        result = self._inner.generate(messages, **kwargs)
        self._agent._record(request, _serialize_chat_message(result))
        return result
```

**Serialization:** ChatMessage contains `tool_calls` (list of ToolCall objects) which must survive Pydantic serialization to SQLite. `_serialize_chat_message` converts to dict; `_deserialize_chat_message` restores.

**Request key:** Uses `n_messages` (message count) rather than full messages (too large) or hash (unnecessary complexity). Sufficient for divergence detection in practice.

---

## 4. HITL Suspend / Resume

Level 1 uses blocking `input()`. Level 2 uses **suspend + persist + resume**:

### Suspend

```python
def execute_tool_call(self, tool_name, arguments):
    request = {"tool_name": tool_name, "arguments": arguments}

    if self.is_replaying:
        record = self._advance_replay(request)
        # Replay budget deduction to keep state consistent
        self._replay_budget(tool_name)
        return record

    # Live: budget deduction
    policy = self.tool_policies.get(tool_name, {})
    if resource := policy.get("resource"):
        self.cap_mgr.deduct(self.capabilities, resource, policy.get("cost", 0.0))

    # HITL gate — suspend, don't block
    if policy.get("destructive", False):
        self._checkpoint.pending_hitl = request
        self._checkpoint.status = "SUSPENDED_FOR_HITL"
        self._save_checkpoint()
        raise HITLSuspendError(self._checkpoint)

    # Execute + record
    result = super().execute_tool_call(tool_name, arguments)
    self._record(request, result)
    return result
```

### Resume

```python
# External code (CLI / API handler):
checkpoint = store.load("agent-001")
checkpoint.pending_hitl = None
checkpoint.status = "RUNNING"
# ... optionally modify the request (HITL_MODIFIED pattern)

agent = CastorResilientAgent(tools=TOOLS, model=real_model, ...,
                             checkpoint=checkpoint)
agent.run(task)  # replays from log, continues live
```

On resume:
1. ReplayModel serves cached LLM responses (no re-calling LLM)
2. execute_tool_call serves cached tool results (no re-executing tools)
3. At the HITL point: `pending_hitl` is None → tool executes live
4. Subsequent calls continue live

### HITLSuspendError

```python
class HITLSuspendError(Exception):
    """Agent suspended for HITL. Checkpoint saved."""
    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
```

---

## 5. Budget Replay

During replay, budget must be re-deducted to keep `capabilities` state consistent:

```python
def _replay_budget(self, tool_name):
    policy = self.tool_policies.get(tool_name, {})
    if resource := policy.get("resource"):
        self.cap_mgr.deduct(self.capabilities, resource, policy.get("cost", 0.0))
```

This ensures that after replay completes, `capabilities.current_usage` reflects the sum of all prior tool costs — so live budget checks work correctly.

---

## 6. File Structure

```
examples/smolagents_guard/
├── guard.py              ← Level 1: Budget + HITL (DONE)
├── deep_guard.py         ← Level 2: + Checkpoint/Replay (NEW)
├── tools.py              ← Shared stub tools (DONE)
├── demo.py               ← Level 1 demo (DONE)
├── demo_deep.py          ← Level 2 demo (NEW)
├── test_guard.py          ← Level 1 tests (DONE)
└── test_deep_guard.py     ← Level 2 tests (NEW)
```

---

## 7. Test Plan

| Test | What It Verifies |
|------|-----------------|
| `test_tool_results_recorded` | `execute_tool_call` appends `SyscallRecord` to `syscall_log` |
| `test_llm_calls_recorded` | `ReplayModel.generate()` appends `SyscallRecord` to `syscall_log` |
| `test_replay_serves_cached_tools` | Resume from checkpoint → tool returns cached, original tool NOT called |
| `test_replay_serves_cached_llm` | Resume from checkpoint → LLM returns cached, inner model NOT called |
| `test_hitl_suspends_and_saves` | Destructive tool raises `HITLSuspendError`, checkpoint has `pending_hitl` |
| `test_hitl_resume_replays_then_continues` | Approve + resume → replay prior calls → HITL point executes live → completes |

---

## 8. Demo Script (`demo_deep.py`) — Two Acts

### Act 1: Crash Recovery

1. Agent executes web_search x2 + write_file x1 (6 syscall records: 3 LLM + 3 tool)
2. Simulate crash (manual interruption)
3. Print: "Agent crashed. 6 syscalls in checkpoint."
4. Resume from checkpoint — new agent, same checkpoint
5. Replay serves all 6 cached results (print REPLAY tag)
6. Continue live: send_message executes
7. Print: "Recovered. 0 LLM calls during replay. 0 tool executions during replay."

### Act 2: HITL Suspend / Resume

1. Agent executes to send_message (destructive)
2. HITLSuspendError → checkpoint saved
3. Print pending_hitl content
4. Approve → modify checkpoint
5. Resume → replay → tool executes → complete
6. Print audit log + budget summary

---

## 9. Design Decisions

| Decision | Rationale |
|----------|-----------|
| Reuse `SyscallRecord` / `AgentCheckpoint` | Users learn Castor's actual data model, not a toy substitute |
| `ReplayModel` wraps Model | Intercepts LLM at the smolagents interface, no monkey-patching |
| `__llm__` as tool_name in request | Distinguishes LLM records from tool records in syscall_log |
| `n_messages` not full messages | Avoids bloating checkpoint; sufficient for divergence detection |
| Budget replayed during replay | Keeps capabilities state consistent for live budget checks |
| `HITLSuspendError` not `SuspendInterrupt` | Clearer name for smolagents context; same semantics |
| Persist after every record | Maximizes crash recovery (at most 1 lost call) |

---

## 10. Known Boundaries

| Limitation | Why | Mitigation |
|-----------|-----|-----------|
| Replay assumes deterministic agent loop | smolagents may vary step count across runs | `ReplayDivergenceError` detects and fails fast |
| ChatMessage serialization may lose detail | Complex nested types in tool_calls | Test serialization round-trip explicitly |
| No preemption | Requires Castor's AgentRunner owning the loop | Out of scope for Level 2 |
| `CheckpointStore` adds SQLAlchemy dependency | Already a Castor core dependency | No new deps |
