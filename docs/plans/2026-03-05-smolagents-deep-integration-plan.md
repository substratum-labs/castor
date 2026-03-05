# smolagents Deep Integration (Level 2) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add checkpoint/replay and HITL suspend/resume to smolagents via `CastorResilientAgent` in `deep_guard.py`.

**Architecture:** Subclass `ToolCallingAgent`, wrap the model with `ReplayModel` to intercept LLM calls, override `execute_tool_call` to intercept tool calls. Both hooks share a single `syscall_log` (Castor's `SyscallRecord` list inside `AgentCheckpoint`). `CheckpointStore` persists to SQLite.

**Tech Stack:** Python 3.11+, smolagents, castor `CapabilityManager` + `SyscallRecord` + `AgentCheckpoint` + `CheckpointStore`

**Design doc:** `docs/plans/2026-03-05-smolagents-deep-integration-design.md`

---

### Task 1: ReplayModel — record and replay LLM calls

**Files:**
- Create: `examples/smolagents_guard/deep_guard.py`

**Step 1: Write deep_guard.py with ReplayModel and serialization helpers**

```python
"""CastorResilientAgent — Level 2 smolagents integration.

Adds checkpoint/replay and HITL suspend/resume to smolagents.
Builds on Level 1 (guard.py) by intercepting both LLM calls and tool calls,
recording them in Castor's native SyscallRecord log.
"""

from __future__ import annotations

from typing import Any

from smolagents import ToolCallingAgent
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, Model

from castor.capability.manager import CapabilityManager
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.stream.persistence import CheckpointStore


# ── ChatMessage serialization ──


def _serialize_chat_message(msg: ChatMessage) -> dict[str, Any]:
    """Convert ChatMessage to a JSON-safe dict for SyscallRecord.response."""
    return msg.dict()


def _deserialize_chat_message(data: dict[str, Any]) -> ChatMessage:
    """Reconstruct ChatMessage from a stored dict."""
    return ChatMessage.from_dict(data)


# ── ReplayModel ──


class ReplayModel(Model):
    """Wraps a smolagents Model to record/replay LLM calls via syscall_log."""

    def __init__(self, inner: Model, agent: CastorResilientAgent):
        super().__init__(model_id=f"castor-replay:{inner.model_id}")
        self._inner = inner
        self._agent = agent

    def generate(self, messages, **kwargs):
        request = {"tool_name": "__llm__", "arguments": {"n_messages": len(messages)}}

        if self._agent.is_replaying:
            data = self._agent._advance_replay(request)
            return _deserialize_chat_message(data)

        result = self._inner.generate(messages, **kwargs)
        self._agent._record(request, _serialize_chat_message(result))
        return result


# ── Errors ──


class HITLSuspendError(Exception):
    """Agent suspended for HITL approval. Checkpoint saved to store."""

    def __init__(self, checkpoint: AgentCheckpoint):
        self.checkpoint = checkpoint
        super().__init__(f"Suspended for HITL: {checkpoint.pending_hitl}")


class ReplayDivergenceError(Exception):
    """Raised when a replay request doesn't match the recorded syscall."""

    def __init__(self, index: int, expected: dict[str, Any], actual: dict[str, Any]):
        self.index = index
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Replay divergence at index {index}: expected {expected}, got {actual}"
        )


# ── CastorResilientAgent ──


class CastorResilientAgent(ToolCallingAgent):
    """Level 2 smolagents integration: budget + HITL + checkpoint/replay.

    Args:
        tools: List of smolagents Tool instances.
        model: The LLM model to use (will be wrapped with ReplayModel).
        budgets: Resource budgets, e.g. ``{"network": 20.0, "disk": 10.0}``.
        tool_policies: Per-tool policy dict.
        checkpoint_store: Optional CheckpointStore for SQLite persistence.
        checkpoint: Optional existing checkpoint to resume from.
        pid: Agent process ID (default: "smolagent-001").
        **kwargs: Passed to ``ToolCallingAgent.__init__``.
    """

    def __init__(
        self,
        tools,
        model,
        budgets: dict[str, float],
        tool_policies: dict[str, dict[str, Any]],
        checkpoint_store: CheckpointStore | None = None,
        checkpoint: AgentCheckpoint | None = None,
        pid: str = "smolagent-001",
        **kwargs,
    ):
        # Create or restore checkpoint
        self.cap_mgr = CapabilityManager()
        if checkpoint is not None:
            self._checkpoint = checkpoint
            # Restore capabilities from checkpoint
            self.capabilities = checkpoint.capabilities
        else:
            self.capabilities = self.cap_mgr.create_capabilities(budgets)
            self._checkpoint = AgentCheckpoint(
                pid=pid,
                status="RUNNING",
                agent_function_name="smolagents_resilient",
                capabilities=self.capabilities,
            )

        self.tool_policies = tool_policies
        self._store = checkpoint_store
        self._replay_index = 0
        self.audit_log: list[dict[str, Any]] = []

        # Wrap model with ReplayModel BEFORE passing to super().__init__
        replay_model = ReplayModel(model, self)
        super().__init__(tools=tools, model=replay_model, **kwargs)

    @property
    def is_replaying(self) -> bool:
        return self._replay_index < len(self._checkpoint.syscall_log)

    def _advance_replay(self, request: dict[str, Any]) -> Any:
        """Serve cached response from syscall_log."""
        record = self._checkpoint.syscall_log[self._replay_index]
        if record.request != request:
            raise ReplayDivergenceError(self._replay_index, record.request, request)
        self._replay_index += 1
        return record.response

    def _record(self, request: dict[str, Any], response: Any) -> None:
        """Append new syscall record and persist checkpoint."""
        self._checkpoint.syscall_log.append(
            SyscallRecord(request=request, response=response)
        )
        self._save_checkpoint()

    def _save_checkpoint(self) -> None:
        if self._store is not None:
            self._store.save(self._checkpoint)

    def _replay_budget(self, tool_name: str) -> None:
        """Re-deduct budget during replay to keep capabilities consistent."""
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

    def execute_tool_call(self, tool_name: str, arguments: dict[str, str] | str) -> Any:
        request = {"tool_name": tool_name, "arguments": arguments}

        # ── Replay path ──
        if self.is_replaying:
            result = self._advance_replay(request)
            self._replay_budget(tool_name)
            self.audit_log.append({
                "tool": tool_name,
                "replayed": True,
            })
            return result

        # ── Live path ──
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate — suspend, don't block
        if policy.get("destructive", False):
            self._checkpoint.pending_hitl = request
            self._checkpoint.status = "SUSPENDED_FOR_HITL"
            self._save_checkpoint()
            raise HITLSuspendError(self._checkpoint)

        # 3. Execute via smolagents
        result = super().execute_tool_call(tool_name, arguments)

        # 4. Record + audit
        self._record(request, result)
        self.audit_log.append({
            "tool": tool_name,
            "cost": cost,
            "resource": resource,
            "replayed": False,
        })
        return result

    def budget_summary(self) -> dict[str, dict[str, float]]:
        return {
            name: {
                "used": cap.current_usage,
                "max": cap.max_budget,
                "remaining": cap.max_budget - cap.current_usage,
            }
            for name, cap in self.capabilities.items()
        }
```

**Step 2: Verify import**

Run: `uv run python -c "import sys; sys.path.insert(0, '.'); from examples.smolagents_guard.deep_guard import CastorResilientAgent, ReplayModel, HITLSuspendError; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add examples/smolagents_guard/deep_guard.py
git commit -m "feat(demo): implement CastorResilientAgent with checkpoint/replay

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Tests — recording and replay

**Files:**
- Create: `examples/smolagents_guard/test_deep_guard.py`

**Step 1: Write test_deep_guard.py**

```python
"""Tests for CastorResilientAgent — checkpoint/replay and HITL suspend/resume."""

from __future__ import annotations

import pytest
from smolagents import tool
from smolagents.models import ChatMessage, Model

from castor.capability.manager import CapabilityExhaustedError

from .deep_guard import (
    CastorResilientAgent,
    HITLSuspendError,
    ReplayDivergenceError,
    _deserialize_chat_message,
    _serialize_chat_message,
)


# ── Stub tools ──


@tool
def safe_tool(query: str) -> str:
    """A safe tool that costs 1.0 network.

    Args:
        query: The input query.
    """
    return f"result for {query}"


@tool
def destructive_tool(target: str) -> str:
    """A destructive tool that requires HITL approval.

    Args:
        target: The target to act on.
    """
    return f"destroyed {target}"


POLICIES = {
    "safe_tool": {"resource": "network", "cost": 1.0},
    "destructive_tool": {"resource": "network", "cost": 2.0, "destructive": True},
}

BUDGETS = {"network": 20.0}


# ── Tracking model ──


class TrackingModel(Model):
    """Model that tracks call count and returns scripted responses."""

    def __init__(self, responses: list[str]):
        super().__init__(model_id="tracking")
        self._responses = responses
        self._call_index = 0
        self.call_count = 0

    def generate(self, messages, **kwargs):
        self.call_count += 1
        idx = self._call_index
        self._call_index += 1
        content = self._responses[idx] if idx < len(self._responses) else "fallback"
        return ChatMessage(role="assistant", content=content)


def make_agent(model=None, checkpoint=None, budgets=None):
    if model is None:
        model = TrackingModel(["response 1", "response 2", "response 3"])
    return CastorResilientAgent(
        tools=[safe_tool, destructive_tool],
        model=model,
        budgets=budgets or BUDGETS,
        tool_policies=POLICIES,
        checkpoint=checkpoint,
    )


# ── Test: ChatMessage serialization round-trip ──


def test_chat_message_serialization_roundtrip():
    original = ChatMessage(role="assistant", content="hello world")
    serialized = _serialize_chat_message(original)
    restored = _deserialize_chat_message(serialized)
    assert restored.role == original.role
    assert restored.content == original.content


# ── Test: recording ──


def test_tool_results_recorded():
    agent = make_agent()
    agent.execute_tool_call("safe_tool", {"query": "test"})
    assert len(agent._checkpoint.syscall_log) == 1
    record = agent._checkpoint.syscall_log[0]
    assert record.request == {"tool_name": "safe_tool", "arguments": {"query": "test"}}
    assert "result for test" in record.response


def test_llm_calls_recorded():
    agent = make_agent()
    # Call the model directly (as smolagents loop would)
    agent.model.generate([{"role": "user", "content": "hi"}])
    assert len(agent._checkpoint.syscall_log) == 1
    record = agent._checkpoint.syscall_log[0]
    assert record.request["tool_name"] == "__llm__"


# ── Test: replay ──


def test_replay_serves_cached_tools():
    # Phase 1: record
    model1 = TrackingModel(["resp1"])
    agent1 = make_agent(model=model1)
    agent1.execute_tool_call("safe_tool", {"query": "test"})
    checkpoint = agent1._checkpoint

    # Phase 2: replay from checkpoint
    model2 = TrackingModel([])  # no responses needed — should be replayed
    agent2 = make_agent(model=model2, checkpoint=checkpoint)
    result = agent2.execute_tool_call("safe_tool", {"query": "test"})

    assert "result for test" in result
    assert agent2.audit_log[0]["replayed"] is True
    # Budget should still be deducted during replay
    assert agent2.capabilities["network"].current_usage == 1.0


def test_replay_serves_cached_llm():
    # Phase 1: record
    model1 = TrackingModel(["original response"])
    agent1 = make_agent(model=model1)
    agent1.model.generate([{"role": "user", "content": "hi"}])
    assert model1.call_count == 1
    checkpoint = agent1._checkpoint

    # Phase 2: replay
    model2 = TrackingModel([])
    agent2 = make_agent(model=model2, checkpoint=checkpoint)
    result = agent2.model.generate([{"role": "user", "content": "hi"}])

    assert result.content == "original response"
    assert model2.call_count == 0  # inner model NOT called


# ── Test: HITL suspend/resume ──


def test_hitl_suspends_and_saves():
    agent = make_agent()
    with pytest.raises(HITLSuspendError) as exc_info:
        agent.execute_tool_call("destructive_tool", {"target": "x"})

    cp = exc_info.value.checkpoint
    assert cp.status == "SUSPENDED_FOR_HITL"
    assert cp.pending_hitl == {"tool_name": "destructive_tool", "arguments": {"target": "x"}}
    # Budget was deducted before suspend
    assert agent.capabilities["network"].current_usage == 2.0


def test_hitl_resume_replays_then_continues():
    # Phase 1: execute safe_tool, then hit HITL on destructive_tool
    model1 = TrackingModel(["plan response"])
    agent1 = make_agent(model=model1)

    # Record an LLM call + safe tool call
    agent1.model.generate([{"role": "user", "content": "do something"}])
    agent1.execute_tool_call("safe_tool", {"query": "research"})

    # Hit HITL
    with pytest.raises(HITLSuspendError):
        agent1.execute_tool_call("destructive_tool", {"target": "send it"})

    checkpoint = agent1._checkpoint

    # Phase 2: approve and resume
    checkpoint.pending_hitl = None
    checkpoint.status = "RUNNING"

    model2 = TrackingModel([])
    agent2 = make_agent(model=model2, checkpoint=checkpoint)

    # Replay LLM call (cached)
    result_llm = agent2.model.generate([{"role": "user", "content": "do something"}])
    assert result_llm.content == "plan response"
    assert model2.call_count == 0  # not re-called

    # Replay safe_tool (cached)
    result_tool = agent2.execute_tool_call("safe_tool", {"query": "research"})
    assert "result for research" in result_tool
    assert agent2.audit_log[0]["replayed"] is True

    # Now live: destructive_tool should execute (HITL cleared)
    # But it's still marked destructive — and pending_hitl is None.
    # We need to handle this: on resume, the HITL-cleared tool should execute.
    # The replay log has NO record for destructive_tool (it suspended before recording).
    # So is_replaying is False → live path → but destructive check → would suspend again!
    #
    # IMPORTANT DESIGN INSIGHT: When resuming from HITL, we need to know that
    # this specific tool call was already approved. We handle this by checking
    # if we just finished replaying and the next request matches the cleared HITL.
    #
    # For now, test that the resume pattern works with a non-destructive follow-up:
    # The key point is that replay worked correctly for the prior calls.
    assert not agent2.is_replaying  # replay exhausted
    assert agent2.capabilities["network"].current_usage == 1.0  # safe_tool replayed budget
```

**Step 2: Run tests**

Run: `uv run pytest examples/smolagents_guard/test_deep_guard.py -v`

Note: Some tests may fail on first run due to:
- ChatMessage serialization edge cases
- ReplayModel constructor interaction with ToolCallingAgent
- HITL resume logic needing refinement

**Step 3: Fix issues iteratively until all tests pass**

Known issue from the test: HITL resume needs special handling — when replay finishes and the next call is the previously-suspended destructive tool, it must execute without re-suspending. The agent needs a `_hitl_approved` flag or must check `_checkpoint.pending_hitl is None` combined with matching the request.

Fix in `execute_tool_call`:

```python
# In the live path, after budget deduction:
if policy.get("destructive", False):
    # Check if this was the HITL-approved call (resume scenario)
    if self._just_finished_replay and self._hitl_cleared_request == request:
        pass  # approved — fall through to execute
    else:
        # New destructive call — suspend
        self._checkpoint.pending_hitl = request
        ...
```

Or simpler: track `_hitl_approved_request` from the cleared `pending_hitl` at init time.

**Step 4: Commit**

```bash
git add examples/smolagents_guard/test_deep_guard.py
git commit -m "test(demo): add deep integration tests for checkpoint/replay + HITL

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Fix HITL resume edge case

**Files:**
- Modify: `examples/smolagents_guard/deep_guard.py`

This task addresses the design insight from Task 2: when resuming from HITL, the previously-suspended destructive tool must execute without re-suspending.

**Step 1: Add `_hitl_approved_request` tracking**

In `__init__`, when restoring from a checkpoint that had HITL cleared:

```python
# In __init__, after checkpoint handling:
self._hitl_approved_request = None
if checkpoint is not None and checkpoint.pending_hitl is None and checkpoint.status == "RUNNING":
    # This checkpoint was HITL-approved — the pending request should execute on resume
    # We detect this by checking if syscall_log has fewer entries than expected
    # (the suspended call was never recorded)
    pass  # _hitl_approved_request set during resume flow
```

Actually, simpler approach: save the cleared `pending_hitl` before clearing it:

```python
# External resume code:
approved_request = checkpoint.pending_hitl  # save before clearing
checkpoint.pending_hitl = None
checkpoint.status = "RUNNING"

agent = CastorResilientAgent(..., checkpoint=checkpoint,
                             hitl_approved_request=approved_request)
```

Add `hitl_approved_request` parameter to `__init__` and check it in `execute_tool_call`.

**Step 2: Update execute_tool_call HITL check**

```python
# In execute_tool_call, live path:
if policy.get("destructive", False):
    if self._hitl_approved_request == request:
        self._hitl_approved_request = None  # consume it
    else:
        self._checkpoint.pending_hitl = request
        self._checkpoint.status = "SUSPENDED_FOR_HITL"
        self._save_checkpoint()
        raise HITLSuspendError(self._checkpoint)
```

**Step 3: Update test_hitl_resume_replays_then_continues to verify full flow**

**Step 4: Run tests**

Run: `uv run pytest examples/smolagents_guard/test_deep_guard.py -v`
Expected: All tests pass

**Step 5: Commit**

```bash
git add examples/smolagents_guard/deep_guard.py examples/smolagents_guard/test_deep_guard.py
git commit -m "fix(demo): handle HITL resume edge case in deep integration

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Demo script

**Files:**
- Create: `examples/smolagents_guard/demo_deep.py`

**Step 1: Write demo_deep.py — two acts**

Act 1: Crash Recovery
- Create agent with TrackingModel
- Execute: LLM call → safe_tool → LLM call → safe_tool → simulate crash
- Resume from checkpoint → all prior calls replayed (REPLAY tag) → continue live
- Print: "0 LLM calls during replay, 0 tool executions during replay"

Act 2: HITL Suspend/Resume
- Create agent, execute until destructive_tool → HITLSuspendError
- Print pending_hitl
- Approve: clear pending_hitl, set status RUNNING, save approved_request
- Resume → replay → destructive tool executes → complete
- Print audit log + budget summary

**Step 2: Run demo**

Run: `uv run python examples/smolagents_guard/demo_deep.py`
Expected: Both acts print clearly

**Step 3: Commit**

```bash
git add examples/smolagents_guard/demo_deep.py
git commit -m "feat(demo): add Level 2 deep integration demo (crash recovery + HITL)

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Verify all tests + push

**Step 1: Run full suite**

Run: `uv run pytest tests/ examples/smolagents_guard/ -q`
Expected: 229+ passed (225 core + 4 L1 guard + 7+ L2 deep)

**Step 2: Lint**

Run: `uv run ruff check src/ examples/smolagents_guard/`
Expected: All checks passed

**Step 3: Push**

Run: `git push`
