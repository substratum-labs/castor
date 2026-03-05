# smolagents Guard Layer Integration — Design

**Date:** 2026-03-05
**Status:** APPROVED
**Goal:** Demonstrate Castor as a security guard layer for smolagents, addressing real security gaps documented in OpenClaw Issue #2023 and NCC Group's smolagents audit.

---

## 1. Strategic Context

### The Problem

smolagents (HuggingFace, ~15k stars) has no built-in:
- Budget/cost enforcement for tool calls
- Human-in-the-loop approval for destructive tools
- Audit trail for tool executions

These gaps mirror OpenClaw's documented incidents (Issue #2023): agents sending messages to wrong recipients, executing destructive operations without approval.

### The Demo Story

"Here's a popular agent framework. Here's its security gap. Add 2 lines of code change (swap `ToolCallingAgent` for `CastorGuardedAgent`), and the problems disappear."

This is part of a two-pronged strategy:
- **Demo 1 (future):** Castor MCP Server for OpenClaw (black-box, drop-in config)
- **Demo 2 (this):** Castor code integration with smolagents (white-box, drives library adoption)

---

## 2. Architecture

### Integration Point

```
smolagents ToolCallingAgent.run(task)
  └→ _step_stream()
       └→ process_tool_calls()
            └→ execute_tool_call(tool_name, arguments)   ← OVERRIDE HERE
                 │
                 ├── Castor budget deduction (CapabilityManager.deduct)
                 ├── Castor HITL gate (if destructive → prompt)
                 ├── Audit log append
                 └── super().execute_tool_call(tool_name, arguments)
```

### What Castor Provides (Guard Layer)

| Feature | Implementation |
|---------|---------------|
| Budget enforcement | `CapabilityManager.deduct()` — hard cap, raises `CapabilityExhaustedError` |
| HITL approval | Interactive prompt for destructive tools, reject → `AgentToolExecutionError` |
| Audit trail | `audit_log` list recording every tool call with cost and resource |

### What Is NOT in Scope (Known Boundaries)

| Feature | Why Not |
|---------|---------|
| Checkpoint/replay | smolagents owns the agent loop — cannot replay from top |
| SyscallProxy pipeline | Requires async; smolagents is sync. Using CapabilityManager directly. |
| Preemption | Requires Castor's AgentRunner controlling the loop |

These boundaries are intentional — they demonstrate where a guard layer hits its limits and full Castor integration becomes necessary.

---

## 3. Configuration

smolagents Tool class has no `destructive`, `cost`, or `resource` metadata. We supply it via a policy dict:

```python
tool_policies = {
    "web_search":   {"resource": "network", "cost": 1.0},
    "read_file":    {"resource": "disk",    "cost": 0.5},
    "write_file":   {"resource": "disk",    "cost": 1.0, "destructive": True},
    "send_message": {"resource": "network", "cost": 2.0, "destructive": True},
}

budgets = {"network": 20.0, "disk": 10.0}
```

### User-Facing API Change

```python
# Before (vanilla smolagents):
agent = ToolCallingAgent(tools=[search, write, send], model=model)

# After (with Castor guard):
agent = CastorGuardedAgent(
    tools=[search, write, send],
    model=model,
    budgets={"network": 20.0, "disk": 10.0},
    tool_policies=tool_policies,
)
```

---

## 4. CastorGuardedAgent Implementation

```python
from smolagents import ToolCallingAgent
from castor.capability.manager import CapabilityManager, CapabilityExhaustedError

class CastorGuardedAgent(ToolCallingAgent):
    def __init__(self, tools, model, budgets, tool_policies, hitl_policy=None, **kwargs):
        super().__init__(tools=tools, model=model, **kwargs)
        self.cap_mgr = CapabilityManager()
        self.capabilities = self.cap_mgr.create_capabilities(budgets)
        self.tool_policies = tool_policies
        self._hitl_policy = hitl_policy  # callable for testing; None → interactive
        self.audit_log = []

    def execute_tool_call(self, tool_name, arguments):
        policy = self.tool_policies.get(tool_name, {})

        # 1. Budget deduction
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate
        if policy.get("destructive", False):
            self._hitl_gate(tool_name, arguments)

        # 3. Original execution
        result = super().execute_tool_call(tool_name, arguments)

        # 4. Audit
        self.audit_log.append({"tool": tool_name, "cost": cost, "resource": resource})
        return result

    def _hitl_gate(self, tool_name, arguments):
        if self._hitl_policy:
            if not self._hitl_policy(tool_name, arguments):
                raise AgentToolExecutionError(f"Rejected: {tool_name}")
            return
        print(f"\n--- CASTOR HITL GATE ---")
        print(f"Tool: {tool_name}")
        print(f"Args: {arguments}")
        choice = input("[a]pprove / [r]eject: ").strip().lower()
        if choice != "a":
            raise AgentToolExecutionError(f"Rejected by human: {tool_name}")
```

### Error Flow

- `CapabilityExhaustedError` → caught by smolagents as tool execution error → agent sees error and adjusts
- HITL rejection → `AgentToolExecutionError` → same path
- No additional try/except needed — smolagents already handles tool errors in `process_tool_calls`

### Sync/Async

- Uses `CapabilityManager` directly (sync), bypasses `SyscallProxy` (async)
- No async bridging needed
- Trade-off: no checkpoint/replay (expected boundary)

---

## 5. Demo Script — Three Acts

### Act 1: Vanilla smolagents (no protection)

- Agent executes `web_search` x3, `write_file` x1, `send_message` x1
- All tools execute without any interception
- Output: "All tools ran. No budget tracking. No approval."

### Act 2: CastorGuardedAgent (budget + HITL)

- Same tools, same task
- `web_search` calls deduct budget normally
- `write_file` triggers HITL → human approves
- `send_message` triggers HITL → human rejects → agent receives error
- Output: budget usage summary + audit log

### Act 3: Budget exhaustion

- Network budget set to 5.0
- Agent searches 3x (3.0) + send_message (2.0) = 5.0
- Next tool call → `CapabilityExhaustedError`
- Output: "Budget exhausted. Agent stopped."

### Mock Model

Demo uses a scripted model that returns predetermined tool calls (no real LLM needed). Tests call `execute_tool_call` directly.

---

## 6. File Structure

```
examples/smolagents_guard/
├── guard.py           ← CastorGuardedAgent (~60 lines)
├── demo.py            ← Three-act demo script
├── tools.py           ← Stub tools (search, write, send)
└── test_guard.py      ← 4 tests: budget deduct, exhausted, HITL reject, HITL approve
```

---

## 7. Test Plan

| Test | What It Verifies |
|------|-----------------|
| `test_budget_deduction` | Tool call correctly deducts from capability budget |
| `test_budget_exhausted` | `CapabilityExhaustedError` raised when budget insufficient |
| `test_hitl_reject` | Destructive tool blocked when human rejects |
| `test_hitl_approve` | Destructive tool executes when human approves |

Tests call `execute_tool_call` directly — no LLM mock needed for unit tests.

---

## 8. Dependencies

- `smolagents` added as dev/optional dependency in `pyproject.toml`
- No changes to Castor core code
- CapabilityManager used as public API

---

## 9. Success Criteria

1. All 4 tests pass
2. Demo script runs end-to-end with scripted model
3. The diff from vanilla smolagents to guarded agent is <=5 lines of user code
4. Existing 225 Castor tests still pass
