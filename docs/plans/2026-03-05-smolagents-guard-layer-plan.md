# smolagents Guard Layer — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a `CastorGuardedAgent` that wraps smolagents' `ToolCallingAgent` with Castor budget enforcement and HITL gates.

**Architecture:** Subclass `ToolCallingAgent`, override `execute_tool_call()` to insert a Castor guard (budget deduction + HITL gate) before delegating to the original tool execution path.

**Tech Stack:** Python 3.11+, smolagents (HuggingFace), castor `CapabilityManager`

**Design doc:** `docs/plans/2026-03-05-smolagents-guard-layer-design.md`

---

### Task 1: Add smolagents dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add smolagents as optional dependency**

In `pyproject.toml`, add a new optional dependency group under `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
observability = [
    "opentelemetry-api>=1.20",
    "opentelemetry-sdk>=1.20",
]
smolagents = [
    "smolagents>=1.0",
]
```

**Step 2: Install**

Run: `uv sync --extra smolagents`
Expected: smolagents installed successfully

**Step 3: Verify import**

Run: `uv run python -c "from smolagents import ToolCallingAgent, tool; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add smolagents as optional dependency"
```

---

### Task 2: Create stub tools

**Files:**
- Create: `examples/smolagents_guard/__init__.py`
- Create: `examples/smolagents_guard/tools.py`

**Step 1: Create directory and __init__.py**

Create empty `examples/smolagents_guard/__init__.py`.

**Step 2: Write tools.py with 4 stub tools**

```python
"""Stub tools for the smolagents guard layer demo.

Four tools mimicking a personal assistant: search, read, write, send.
"""

from smolagents import tool


@tool
def web_search(query: str) -> str:
    """Search the web and return result snippets.

    Args:
        query: The search query string.
    """
    return f"[Result 1] Wikipedia: {query}\n[Result 2] Blog post about {query}"


@tool
def read_file(filename: str) -> str:
    """Read a file from the knowledge base.

    Args:
        filename: Name of the file to read.
    """
    return f"Contents of {filename}: (stub data)"


@tool
def write_file(filename: str, content: str) -> str:
    """Write content to a file in the knowledge base (destructive).

    Args:
        filename: Name of the file to write.
        content: The content to write.
    """
    return f"Saved '{filename}' ({len(content)} chars)."


@tool
def send_message(recipient: str, body: str) -> str:
    """Send a message to a recipient on Slack (destructive, requires approval).

    Args:
        recipient: The recipient channel or user.
        body: The message body.
    """
    return f"Message sent to {recipient}: {body[:50]}..."
```

**Step 3: Verify tools load**

Run: `uv run python -c "from examples.smolagents_guard.tools import web_search, send_message; print(web_search.name, send_message.name)"`
Expected: `web_search send_message`

**Step 4: Commit**

```bash
git add examples/smolagents_guard/__init__.py examples/smolagents_guard/tools.py
git commit -m "feat(demo): add smolagents stub tools for guard layer demo"
```

---

### Task 3: Implement CastorGuardedAgent

**Files:**
- Create: `examples/smolagents_guard/guard.py`

**Step 1: Write guard.py**

```python
"""CastorGuardedAgent — smolagents + Castor security guard layer.

Subclasses smolagents ToolCallingAgent to add:
- Budget enforcement via Castor CapabilityManager
- HITL gates for destructive tool calls
- Audit logging of all tool executions
"""

from __future__ import annotations

from typing import Any

from smolagents import ToolCallingAgent

from castor.capability.manager import CapabilityManager, CapabilityExhaustedError


class CastorGuardedAgent(ToolCallingAgent):
    """A smolagents ToolCallingAgent with Castor security guardrails.

    Args:
        tools: List of smolagents Tool instances.
        model: The LLM model to use.
        budgets: Resource budgets, e.g. ``{"network": 20.0, "disk": 10.0}``.
        tool_policies: Per-tool policy dict mapping tool name to
            ``{"resource": str, "cost": float, "destructive": bool}``.
        hitl_policy: Optional callable ``(tool_name, arguments) -> bool`` for
            programmatic HITL decisions. ``None`` means interactive prompt.
        **kwargs: Passed to ``ToolCallingAgent.__init__``.
    """

    def __init__(
        self,
        tools,
        model,
        budgets: dict[str, float],
        tool_policies: dict[str, dict[str, Any]],
        hitl_policy=None,
        **kwargs,
    ):
        super().__init__(tools=tools, model=model, **kwargs)
        self.cap_mgr = CapabilityManager()
        self.capabilities = self.cap_mgr.create_capabilities(budgets)
        self.tool_policies = tool_policies
        self._hitl_policy = hitl_policy
        self.audit_log: list[dict[str, Any]] = []

    def execute_tool_call(self, tool_name: str, arguments: dict[str, str] | str) -> Any:
        policy = self.tool_policies.get(tool_name, {})
        resource = policy.get("resource")
        cost = policy.get("cost", 0.0)

        # 1. Budget deduction — hard cap
        if resource:
            self.cap_mgr.deduct(self.capabilities, resource, cost)

        # 2. HITL gate — destructive tools require human approval
        if policy.get("destructive", False):
            self._hitl_gate(tool_name, arguments)

        # 3. Execute via smolagents original path
        result = super().execute_tool_call(tool_name, arguments)

        # 4. Audit
        self.audit_log.append({
            "tool": tool_name,
            "cost": cost,
            "resource": resource,
        })
        return result

    def _hitl_gate(self, tool_name: str, arguments: dict[str, str] | str) -> None:
        if self._hitl_policy is not None:
            if not self._hitl_policy(tool_name, arguments):
                raise ToolRejectedError(tool_name)
            return
        # Interactive mode
        print(f"\n--- CASTOR HITL GATE ---")
        print(f"Tool: {tool_name}")
        print(f"Args: {arguments}")
        choice = input("[a]pprove / [r]eject: ").strip().lower()
        if choice != "a":
            raise ToolRejectedError(tool_name)

    def budget_summary(self) -> dict[str, dict[str, float]]:
        """Return current budget usage for display."""
        return {
            name: {
                "used": cap.current_usage,
                "max": cap.max_budget,
                "remaining": cap.max_budget - cap.current_usage,
            }
            for name, cap in self.capabilities.items()
        }


class ToolRejectedError(Exception):
    """Raised when a human rejects a destructive tool call via HITL gate."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' rejected by human via HITL gate")
```

**Step 2: Verify import**

Run: `uv run python -c "from examples.smolagents_guard.guard import CastorGuardedAgent; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add examples/smolagents_guard/guard.py
git commit -m "feat(demo): implement CastorGuardedAgent with budget + HITL guard"
```

---

### Task 4: Write tests

**Files:**
- Create: `examples/smolagents_guard/test_guard.py`

**Step 1: Write test_guard.py**

Tests call `execute_tool_call` directly — no LLM model needed. We construct the agent with tools registered, then call `execute_tool_call` to test the guard layer.

```python
"""Tests for CastorGuardedAgent — budget enforcement and HITL gates."""

from __future__ import annotations

import pytest
from smolagents import tool

from castor.capability.manager import CapabilityExhaustedError

from .guard import CastorGuardedAgent, ToolRejectedError


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


class FakeModel:
    """Minimal model stub satisfying ToolCallingAgent.__init__."""

    model_id = "fake"

    def generate(self, messages, **kwargs):
        from smolagents.models import ChatMessage

        return ChatMessage(role="assistant", content="done")


def make_agent(budgets, hitl_policy=None):
    return CastorGuardedAgent(
        tools=[safe_tool, destructive_tool],
        model=FakeModel(),
        budgets=budgets,
        tool_policies=POLICIES,
        hitl_policy=hitl_policy,
    )


# ── Tests ──


def test_budget_deduction():
    agent = make_agent(budgets={"network": 10.0})
    agent.execute_tool_call("safe_tool", {"query": "test"})
    assert agent.capabilities["network"].current_usage == 1.0
    assert len(agent.audit_log) == 1
    assert agent.audit_log[0]["tool"] == "safe_tool"


def test_budget_exhausted():
    agent = make_agent(budgets={"network": 0.5})
    with pytest.raises(CapabilityExhaustedError):
        agent.execute_tool_call("safe_tool", {"query": "test"})
    # Tool should NOT have executed — no audit entry
    assert len(agent.audit_log) == 0


def test_hitl_reject():
    agent = make_agent(
        budgets={"network": 10.0},
        hitl_policy=lambda name, args: False,
    )
    with pytest.raises(ToolRejectedError):
        agent.execute_tool_call("destructive_tool", {"target": "x"})
    # Budget was deducted but tool didn't execute
    assert agent.capabilities["network"].current_usage == 2.0
    assert len(agent.audit_log) == 0


def test_hitl_approve():
    agent = make_agent(
        budgets={"network": 10.0},
        hitl_policy=lambda name, args: True,
    )
    result = agent.execute_tool_call("destructive_tool", {"target": "x"})
    assert "destroyed" in result
    assert agent.capabilities["network"].current_usage == 2.0
    assert len(agent.audit_log) == 1
```

**Step 2: Run tests**

Run: `uv run pytest examples/smolagents_guard/test_guard.py -v`
Expected: 4 passed

Note: The `FakeModel` stub may need adjustment depending on smolagents' `ToolCallingAgent.__init__` requirements. If `__init__` calls methods on model that `FakeModel` doesn't implement, add minimal stubs. This is expected exploration — we're experimenting.

**Step 3: Fix any issues discovered**

If smolagents requires additional model interface methods or if `execute_tool_call` has dependencies we didn't expect (e.g., `self.tools` dict structure, argument validation), fix them here.

**Step 4: Commit**

```bash
git add examples/smolagents_guard/test_guard.py
git commit -m "test(demo): add guard layer tests for budget + HITL"
```

---

### Task 5: Verify existing tests still pass

**Files:** None (verification only)

**Step 1: Run full Castor test suite**

Run: `uv run pytest tests/ -q`
Expected: 225 passed

**Step 2: Run lint**

Run: `uv run ruff check src/ examples/smolagents_guard/`
Expected: All checks passed

---

### Task 6: Demo script (optional, if time permits)

**Files:**
- Create: `examples/smolagents_guard/demo.py`

**Step 1: Write demo.py**

A script that runs the three-act demo. This requires a mock model that returns predetermined tool calls. The exact mock depends on smolagents' `ChatMessage` format for tool calls, which we'll discover during implementation.

The demo script is lower priority than the guard + tests. If the mock model proves complex, defer the demo and focus on the guard + test as the deliverable.

**Step 2: Run demo**

Run: `uv run python examples/smolagents_guard/demo.py`
Expected: Three acts print their output

**Step 3: Commit**

```bash
git add examples/smolagents_guard/demo.py
git commit -m "feat(demo): add three-act smolagents guard demo script"
```

---

### Task 7: Final commit and push

**Step 1: Push all commits**

Run: `git push`

---

## Risk Notes

1. **smolagents API surface**: We're hooking into `execute_tool_call` which is a public method but not documented as a stable API. If smolagents changes it, the guard breaks. This is acceptable for a demo.

2. **FakeModel compatibility**: `ToolCallingAgent.__init__` may call methods on the model (e.g., `model.supports_tool_calling()`). We'll discover and fix during Task 4.

3. **CapabilityExhaustedError propagation**: smolagents catches `Exception` in `execute_tool_call` and wraps it as `AgentToolExecutionError`. Our `CapabilityExhaustedError` is raised BEFORE `super().execute_tool_call()`, so it propagates OUTSIDE the try/except. It will reach `process_tool_calls` uncaught. We may need to wrap it — discover during testing.
