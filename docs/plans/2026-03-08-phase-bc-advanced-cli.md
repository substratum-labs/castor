# Phase B Advanced + Phase C CLI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add high-level agent patterns (parallel, react, map_reduce, plan_execute, conversation, supervisor, run_task) to `castor.lib` and replace the single-file CLI with a full `cli/` package supporting `castor run`, `castor ps`, `castor approve`, etc.

**Architecture:** Phase B patterns are pure Python functions built on existing `castor.lib` primitives (`tool()`, `chat()`, `spawn()`, `join()`). They use ContextVar to access the proxy implicitly. Phase C CLI uses argparse with subcommands, dynamically loading agent modules via importlib. Implementation order: Phase B first (patterns are kernel capability), then Phase C (thin shell).

**Tech Stack:** Python 3.11+, asyncio, argparse, importlib, pydantic, pytest, pytest-asyncio

---

## Context for Implementer

**Existing `castor.lib` primitives** (in `src/castor/lib/primitives.py`):
```python
async def tool(name: str, /, **kwargs: Any) -> Any          # call registered tool
async def chat(prompt: str, *, system: str = "", tool_name: str = "llm_inference") -> str  # call LLM tool
def budget(resource: str) -> float                           # check remaining budget
async def try_tool(name: str, /, **kwargs: Any) -> Any       # semantic alias for tool()
```

**Existing `castor.lib` spawn** (in `src/castor/lib/spawn.py`):
```python
async def spawn(agent_name: str, *, capabilities: dict[str, float] | None = None) -> str  # async spawn
async def join(handle: str) -> Any                           # await child result
```

**ContextVar bridge** (`src/castor/lib/_context.py`): `get_proxy()` returns the current `SyscallProxy` set by `AgentRunner` via `set_proxy()`. All `castor.lib` functions use this.

**Test pattern** (from `tests/test_lib_primitives.py`): Register mock tools via `@castor_tool`, create `SyscallProxy` with `AgentCheckpoint`, call `set_proxy(proxy)`, then test lib functions. No real LLM needed — mock LLM is just a `@castor_tool` that returns preset strings.

**Tool registry access**: `proxy._gate.registry.list_tools()` returns sorted list of registered tool names. `proxy._gate.registry.has_tool(name)` checks existence.

**Existing CLI** (`src/castor/cli.py`): Single file with argparse, 184 lines. Commands: `list`, `show`, `reject`, `modify`. Entry point in pyproject.toml: `castor = "castor.cli:main"`.

---

### Task 1: `parallel()` — concurrent tool execution

**Files:**
- Create: `tests/test_lib_patterns.py`
- Modify: `src/castor/lib/patterns.py` (create new file)
- Modify: `src/castor/lib/__init__.py:1-13`

**Step 1: Write the failing test**

Create `tests/test_lib_patterns.py`:

```python
"""Tests for castor.lib.patterns — parallel, react, map_reduce, etc."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.lib._context import set_proxy
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.proxy import SyscallProxy


@pytest.fixture()
def registry():
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    @castor_tool(consumes="api", cost_per_use=1.0)
    def summarize(text: str) -> str:
        return f"summary of {text}"

    reg.register(search._castor_metadata)
    reg.register(summarize._castor_metadata)
    return reg


@pytest.fixture()
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def proxy(gate, cap_mgr):
    cp = AgentCheckpoint(
        pid="test-patterns-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)
    return p


@pytest.mark.asyncio()
async def test_parallel_executes_multiple_tools(proxy):
    from castor.lib.patterns import parallel

    results = await parallel(
        ("search", {"query": "a"}),
        ("summarize", {"text": "b"}),
    )
    assert results == ["results for a", "summary of b"]


@pytest.mark.asyncio()
async def test_parallel_empty(proxy):
    from castor.lib.patterns import parallel

    results = await parallel()
    assert results == []


@pytest.mark.asyncio()
async def test_parallel_single(proxy):
    from castor.lib.patterns import parallel

    results = await parallel(("search", {"query": "x"}))
    assert results == ["results for x"]
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_patterns.py::test_parallel_executes_multiple_tools -v`
Expected: FAIL with "ModuleNotFoundError" or "ImportError" (patterns.py doesn't exist)

**Step 3: Write minimal implementation**

Create `src/castor/lib/patterns.py`:

```python
"""High-level agent patterns built on castor.lib primitives."""

from __future__ import annotations

from typing import Any

from castor.lib.primitives import tool


async def parallel(*tool_calls: tuple[str, dict[str, Any]]) -> list[Any]:
    """Execute multiple tool calls sequentially, return results in order.

    Each element is (tool_name, arguments_dict).
    Note: currently sequential — future versions may use spawn/join for true
    concurrency when tools support it.
    """
    results = []
    for name, args in tool_calls:
        result = await tool(name, **args)
        results.append(result)
    return results
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_patterns.py -k parallel -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add src/castor/lib/patterns.py tests/test_lib_patterns.py
git commit -m "feat: add parallel() pattern to castor.lib"
```

---

### Task 2: `react()` — ReAct loop

**Files:**
- Modify: `tests/test_lib_patterns.py`
- Modify: `src/castor/lib/patterns.py`

**Step 1: Write the failing test**

Add to `tests/test_lib_patterns.py` — need a mock LLM tool that returns preset ACTION/FINISH sequences:

```python
@pytest.fixture()
def registry_with_llm():
    """Registry with search + a mock LLM that follows a scripted sequence."""
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    # Mock LLM that returns a scripted sequence based on call count
    call_count = {"n": 0}
    script = [
        'THOUGHT: I need to search\nACTION: search({"query": "test"})',
        "THOUGHT: Got results\nFINISH: done with results for test",
    ]

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        idx = call_count["n"]
        call_count["n"] += 1
        return script[idx]

    reg.register(search._castor_metadata)
    reg.register(llm_inference._castor_metadata)
    return reg


@pytest.fixture()
def proxy_with_llm(registry_with_llm, cap_mgr):
    gate = SyscallGate(registry_with_llm)
    cp = AgentCheckpoint(
        pid="test-react-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)
    return p


@pytest.mark.asyncio()
async def test_react_basic(proxy_with_llm):
    from castor.lib.patterns import react

    result = await react("find test info", tools=["search"])
    assert "results for test" in result


@pytest.mark.asyncio()
async def test_react_max_steps_exceeded(cap_mgr):
    """react() raises RuntimeError when max_steps exceeded without FINISH."""
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        return 'THOUGHT: thinking\nACTION: noop({})'

    @castor_tool(consumes="api", cost_per_use=0.0)
    def noop() -> str:
        return "ok"

    reg.register(llm_inference._castor_metadata)
    reg.register(noop._castor_metadata)
    gate = SyscallGate(reg)
    cp = AgentCheckpoint(
        pid="test-react-max",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)

    from castor.lib.patterns import react

    with pytest.raises(RuntimeError, match="max_steps"):
        await react("goal", tools=["noop"], max_steps=2)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_patterns.py::test_react_basic -v`
Expected: FAIL with "ImportError: cannot import name 'react'"

**Step 3: Write minimal implementation**

Add to `src/castor/lib/patterns.py`:

```python
import json
import re

from castor.lib.primitives import chat, tool


async def react(
    goal: str,
    tools: list[str],
    *,
    max_steps: int = 10,
    tool_name: str = "llm_inference",
) -> str:
    """ReAct loop: Think -> Act -> Observe, until LLM outputs FINISH.

    The LLM is prompted to output one of:
    - ACTION: tool_name({"arg": "value"})
    - FINISH: final_answer

    Args:
        goal: The task description for the LLM.
        tools: List of tool names the LLM may use.
        max_steps: Maximum think-act-observe cycles.
        tool_name: Name of the registered LLM tool.
    """
    observations: list[str] = []
    system = (
        f"You are a ReAct agent. Available tools: {tools}\n"
        "On each step respond with EXACTLY one of:\n"
        '  ACTION: tool_name({{"arg": "value"}})\n'
        "  FINISH: your_final_answer\n"
        "Do NOT output anything else."
    )

    for step in range(max_steps):
        if observations:
            prompt = f"Goal: {goal}\n\nHistory:\n" + "\n".join(observations) + "\n\nNext step:"
        else:
            prompt = f"Goal: {goal}\n\nNext step:"

        response = await chat(prompt, system=system, tool_name=tool_name)

        # Parse FINISH
        finish_match = re.search(r"FINISH:\s*(.+)", response, re.DOTALL)
        if finish_match:
            return finish_match.group(1).strip()

        # Parse ACTION
        action_match = re.search(r"ACTION:\s*(\w+)\((.+?)\)\s*$", response, re.DOTALL)
        if action_match:
            act_tool = action_match.group(1)
            try:
                act_args = json.loads(action_match.group(2))
            except json.JSONDecodeError:
                act_args = {}

            if act_tool not in tools:
                observations.append(f"Step {step + 1}: ERROR — tool {act_tool!r} not in allowed tools")
                continue

            result = await tool(act_tool, **act_args)
            observations.append(f"Step {step + 1}: {act_tool}({act_args}) -> {result}")
        else:
            observations.append(f"Step {step + 1}: Could not parse response: {response}")

    raise RuntimeError(f"react() exceeded max_steps={max_steps} without FINISH")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_patterns.py -k react -v`
Expected: 2 PASSED

**Step 5: Commit**

```bash
git add src/castor/lib/patterns.py tests/test_lib_patterns.py
git commit -m "feat: add react() ReAct loop pattern to castor.lib"
```

---

### Task 3: `map_reduce()` — parallel map + reduce

**Files:**
- Modify: `tests/test_lib_patterns.py`
- Modify: `src/castor/lib/patterns.py`

**Step 1: Write the failing test**

Add to `tests/test_lib_patterns.py`:

```python
@pytest.mark.asyncio()
async def test_map_reduce(proxy):
    """map_reduce maps each item through map_tool, then reduces."""
    from castor.lib.patterns import map_reduce

    # search is our map_tool (query=item), summarize is reduce_tool (text=joined)
    result = await map_reduce(
        items=["a", "b", "c"],
        map_tool="search",
        map_args_fn=lambda item: {"query": item},
        reduce_tool="summarize",
        reduce_args_fn=lambda results: {"text": " | ".join(str(r) for r in results)},
    )
    assert result == "summary of results for a | results for b | results for c"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_patterns.py::test_map_reduce -v`
Expected: FAIL with "ImportError: cannot import name 'map_reduce'"

**Step 3: Write minimal implementation**

Add to `src/castor/lib/patterns.py`:

```python
from collections.abc import Callable


async def map_reduce(
    items: list[Any],
    map_tool: str,
    reduce_tool: str,
    *,
    map_args_fn: Callable[[Any], dict[str, Any]] | None = None,
    reduce_args_fn: Callable[[list[Any]], dict[str, Any]] | None = None,
) -> Any:
    """Map each item through map_tool, then reduce all results with reduce_tool.

    Args:
        items: List of items to process.
        map_tool: Tool name to apply to each item.
        reduce_tool: Tool name to aggregate results.
        map_args_fn: Converts an item to tool kwargs. Default: {"item": item}.
        reduce_args_fn: Converts result list to tool kwargs. Default: {"items": results}.
    """
    if map_args_fn is None:
        map_args_fn = lambda item: {"item": item}
    if reduce_args_fn is None:
        reduce_args_fn = lambda results: {"items": results}

    # Map phase
    map_results = []
    for item in items:
        result = await tool(map_tool, **map_args_fn(item))
        map_results.append(result)

    # Reduce phase
    return await tool(reduce_tool, **reduce_args_fn(map_results))
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_patterns.py::test_map_reduce -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/castor/lib/patterns.py tests/test_lib_patterns.py
git commit -m "feat: add map_reduce() pattern to castor.lib"
```

---

### Task 4: `plan_execute()` — plan then execute

**Files:**
- Modify: `tests/test_lib_patterns.py`
- Modify: `src/castor/lib/patterns.py`

**Step 1: Write the failing test**

Add to `tests/test_lib_patterns.py`:

```python
@pytest.mark.asyncio()
async def test_plan_execute(cap_mgr):
    """plan_execute: LLM generates a plan, then executor runs each step."""
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    @castor_tool(consumes="api", cost_per_use=1.0)
    def summarize(text: str) -> str:
        return f"summary of {text}"

    call_count = {"n": 0}
    responses = [
        # Planner call: return JSON plan
        '[{"tool": "search", "args": {"query": "data"}}, {"tool": "summarize", "args": {"text": "data"}}]',
        # Final summary call
        "FINISH: executed 2 steps successfully",
    ]

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        idx = call_count["n"]
        call_count["n"] += 1
        return responses[idx]

    reg.register(search._castor_metadata)
    reg.register(summarize._castor_metadata)
    reg.register(llm_inference._castor_metadata)
    gate = SyscallGate(reg)
    cp = AgentCheckpoint(
        pid="test-planexec-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)

    from castor.lib.patterns import plan_execute

    result = await plan_execute(
        "analyze data",
        executor_tools=["search", "summarize"],
    )
    assert "executed 2 steps" in result
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_patterns.py::test_plan_execute -v`
Expected: FAIL with "ImportError: cannot import name 'plan_execute'"

**Step 3: Write minimal implementation**

Add to `src/castor/lib/patterns.py`:

```python
async def plan_execute(
    goal: str,
    executor_tools: list[str],
    *,
    tool_name: str = "llm_inference",
) -> str:
    """Plan then execute: LLM generates a step list, then executes each step.

    The planner LLM is asked to return a JSON list of steps:
    [{"tool": "name", "args": {...}}, ...]

    After executing all steps, the LLM summarizes the results.

    Args:
        goal: The task description.
        executor_tools: List of tool names the executor may use.
        tool_name: Name of the registered LLM tool.
    """
    # Phase 1: Plan
    plan_prompt = (
        f"Goal: {goal}\n"
        f"Available tools: {executor_tools}\n"
        "Return a JSON array of steps. Each step: "
        '{"tool": "tool_name", "args": {"key": "value"}}\n'
        "Return ONLY the JSON array, nothing else."
    )
    plan_response = await chat(plan_prompt, tool_name=tool_name)

    try:
        steps = json.loads(plan_response)
    except json.JSONDecodeError:
        return f"ERROR: Could not parse plan: {plan_response}"

    # Phase 2: Execute
    step_results = []
    for i, step in enumerate(steps):
        step_tool = step.get("tool", "")
        step_args = step.get("args", {})
        if step_tool not in executor_tools:
            step_results.append(f"Step {i + 1}: SKIPPED — {step_tool!r} not allowed")
            continue
        result = await tool(step_tool, **step_args)
        step_results.append(f"Step {i + 1}: {step_tool}({step_args}) -> {result}")

    # Phase 3: Summarize
    summary_prompt = (
        f"Goal: {goal}\n"
        f"Execution results:\n" + "\n".join(step_results) + "\n"
        "Summarize the outcome. Start with FINISH:"
    )
    summary = await chat(summary_prompt, tool_name=tool_name)
    finish_match = re.search(r"FINISH:\s*(.+)", summary, re.DOTALL)
    return finish_match.group(1).strip() if finish_match else summary
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_patterns.py::test_plan_execute -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/castor/lib/patterns.py tests/test_lib_patterns.py
git commit -m "feat: add plan_execute() pattern to castor.lib"
```

---

### Task 5: `conversation()` — multi-turn chat loop

**Files:**
- Modify: `tests/test_lib_patterns.py`
- Modify: `src/castor/lib/patterns.py`

**Step 1: Write the failing test**

Add to `tests/test_lib_patterns.py`:

```python
@pytest.mark.asyncio()
async def test_conversation(cap_mgr):
    """conversation: multi-turn user_input -> LLM loop."""
    reg = ToolRegistry()

    input_count = {"n": 0}
    user_inputs = ["hello", "EXIT"]

    @castor_tool(consumes="_default", cost_per_use=0.0)
    def user_input() -> str:
        idx = input_count["n"]
        input_count["n"] += 1
        return user_inputs[idx]

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        return f"echo: {prompt}"

    reg.register(user_input._castor_metadata)
    reg.register(llm_inference._castor_metadata)
    gate = SyscallGate(reg)
    cp = AgentCheckpoint(
        pid="test-convo-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)

    from castor.lib.patterns import conversation

    history = await conversation("You are a helpful assistant.", exit_word="EXIT")
    # Should have 1 exchange (hello -> echo) before EXIT
    assert len(history) == 2  # user msg + assistant msg
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hello"
    assert history[1]["role"] == "assistant"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_patterns.py::test_conversation -v`
Expected: FAIL with "ImportError: cannot import name 'conversation'"

**Step 3: Write minimal implementation**

Add to `src/castor/lib/patterns.py`:

```python
async def conversation(
    system: str,
    *,
    max_turns: int = 20,
    tool_name: str = "llm_inference",
    input_tool: str = "user_input",
    exit_word: str = "EXIT",
) -> list[dict[str, str]]:
    """Multi-turn chat: user_input -> LLM -> repeat until exit_word or max_turns.

    Args:
        system: System prompt for the LLM.
        max_turns: Maximum conversation exchanges.
        tool_name: Name of the registered LLM tool.
        input_tool: Name of the tool that gets user input.
        exit_word: User input that ends the conversation.

    Returns:
        List of {"role": "user"/"assistant", "content": "..."} dicts.
    """
    history: list[dict[str, str]] = []

    for _ in range(max_turns):
        user_msg = await tool(input_tool)
        if user_msg == exit_word:
            break

        history.append({"role": "user", "content": str(user_msg)})

        # Build prompt from history
        prompt = "\n".join(
            f"{m['role']}: {m['content']}" for m in history
        )
        response = await chat(prompt, system=system, tool_name=tool_name)
        history.append({"role": "assistant", "content": response})

    return history
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_patterns.py::test_conversation -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/castor/lib/patterns.py tests/test_lib_patterns.py
git commit -m "feat: add conversation() multi-turn chat pattern to castor.lib"
```

---

### Task 6: `supervisor()` — multi-agent delegation

**Files:**
- Modify: `tests/test_lib_patterns.py`
- Modify: `src/castor/lib/patterns.py`

**Step 1: Write the failing test**

Add to `tests/test_lib_patterns.py`:

```python
@pytest.mark.asyncio()
async def test_supervisor(cap_mgr):
    """supervisor: LLM picks agent, spawn/join, repeat until FINISH."""
    reg = ToolRegistry()
    agent_reg = AgentRegistry()

    from castor.scheduler.agent_registry import AgentRegistry

    agent_reg = AgentRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    call_count = {"n": 0}
    llm_responses = [
        'DELEGATE: researcher',
        'FINISH: researcher found results for task',
    ]

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        idx = call_count["n"]
        call_count["n"] += 1
        return llm_responses[idx]

    reg.register(search._castor_metadata)
    reg.register(llm_inference._castor_metadata)

    async def researcher_agent(proxy):
        return await proxy.syscall("search", query="task")

    agent_reg.register("researcher", researcher_agent)

    gate = SyscallGate(reg)
    cp = AgentCheckpoint(
        pid="test-supervisor-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr, agent_registry=agent_reg)
    set_proxy(p)

    from castor.lib.patterns import supervisor

    result = await supervisor(
        "find research data",
        agents=["researcher"],
    )
    assert "results for task" in result
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_patterns.py::test_supervisor -v`
Expected: FAIL with "ImportError: cannot import name 'supervisor'"

**Step 3: Write minimal implementation**

Add to `src/castor/lib/patterns.py`:

```python
from castor.lib.spawn import join, spawn


async def supervisor(
    task: str,
    agents: list[str],
    *,
    tool_name: str = "llm_inference",
    max_rounds: int = 5,
) -> str:
    """Supervisor pattern: LLM decides which agent to delegate to.

    The LLM outputs one of:
    - DELEGATE: agent_name
    - FINISH: final_answer

    Args:
        task: The task description.
        agents: List of available agent names.
        tool_name: Name of the registered LLM tool.
        max_rounds: Maximum delegation rounds.
    """
    results: list[str] = []
    system = (
        f"You are a supervisor. Available agents: {agents}\n"
        "On each round respond with EXACTLY one of:\n"
        "  DELEGATE: agent_name\n"
        "  FINISH: your_final_answer\n"
    )

    for round_num in range(max_rounds):
        if results:
            prompt = f"Task: {task}\n\nAgent results so far:\n" + "\n".join(results) + "\n\nNext action:"
        else:
            prompt = f"Task: {task}\n\nNext action:"

        response = await chat(prompt, system=system, tool_name=tool_name)

        # Parse FINISH
        finish_match = re.search(r"FINISH:\s*(.+)", response, re.DOTALL)
        if finish_match:
            return finish_match.group(1).strip()

        # Parse DELEGATE
        delegate_match = re.search(r"DELEGATE:\s*(\w+)", response)
        if delegate_match:
            agent_name = delegate_match.group(1)
            if agent_name not in agents:
                results.append(f"Round {round_num + 1}: ERROR — agent {agent_name!r} not available")
                continue
            handle = await spawn(agent_name)
            agent_result = await join(handle)
            results.append(f"Round {round_num + 1}: {agent_name} -> {agent_result}")
        else:
            results.append(f"Round {round_num + 1}: Could not parse: {response}")

    raise RuntimeError(f"supervisor() exceeded max_rounds={max_rounds} without FINISH")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_patterns.py::test_supervisor -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/castor/lib/patterns.py tests/test_lib_patterns.py
git commit -m "feat: add supervisor() multi-agent delegation pattern to castor.lib"
```

---

### Task 7: `run_task()` — Level 0 API

**Files:**
- Create: `src/castor/lib/run_task.py`
- Create: `tests/test_lib_run_task.py`

**Step 1: Write the failing test**

Create `tests/test_lib_run_task.py`:

```python
"""Tests for castor.lib.run_task — Level 0 API."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.lib._context import set_proxy
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.proxy import SyscallProxy


@pytest.mark.asyncio()
async def test_run_task_basic():
    cap_mgr = CapabilityManager()
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    call_count = {"n": 0}
    script = [
        'THOUGHT: Search for info\nACTION: search({"query": "hello"})',
        "THOUGHT: Got it\nFINISH: found results for hello",
    ]

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        idx = call_count["n"]
        call_count["n"] += 1
        return script[idx]

    reg.register(search._castor_metadata)
    reg.register(llm_inference._castor_metadata)
    gate = SyscallGate(reg)
    cp = AgentCheckpoint(
        pid="test-runtask-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)

    from castor.lib.run_task import run_task

    result = await run_task("find info about hello")
    assert "results for hello" in result


@pytest.mark.asyncio()
async def test_run_task_auto_discovers_tools():
    """run_task with tools=None discovers all non-LLM tools."""
    cap_mgr = CapabilityManager()
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def calc(expression: str) -> str:
        return f"result: {expression}"

    call_count = {"n": 0}

    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        idx = call_count["n"]
        call_count["n"] += 1
        if idx == 0:
            # Verify the tool list includes calc but not llm_inference
            assert "calc" in prompt
            return 'THOUGHT: use calc\nACTION: calc({"expression": "1+1"})'
        return "FINISH: computed 1+1"

    reg.register(calc._castor_metadata)
    reg.register(llm_inference._castor_metadata)
    gate = SyscallGate(reg)
    cp = AgentCheckpoint(
        pid="test-runtask-auto",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)

    from castor.lib.run_task import run_task

    # tools=None should auto-discover calc (exclude llm_inference)
    result = await run_task("compute 1+1")
    assert "1+1" in result
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_run_task.py::test_run_task_basic -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

Create `src/castor/lib/run_task.py`:

```python
"""run_task: Level 0 API — one-sentence goal, auto ReAct execution."""

from __future__ import annotations

from typing import Any

from castor.lib._context import get_proxy
from castor.lib.patterns import react


async def run_task(
    goal: str,
    *,
    tools: list[str] | None = None,
    max_steps: int = 10,
    tool_name: str = "llm_inference",
) -> str:
    """Level 0 API: describe a goal, get a result.

    Wraps react() with automatic tool discovery.

    Args:
        goal: Natural language description of the task.
        tools: Explicit tool list. None = auto-discover from Gate.
        max_steps: Maximum ReAct steps.
        tool_name: Name of the registered LLM tool.

    Raises:
        RuntimeError: If no LLM tool is registered or max_steps exceeded.
    """
    if tools is None:
        proxy = get_proxy()
        all_tools = proxy._gate.registry.list_tools()
        tools = [t for t in all_tools if t != tool_name]

    if not tools:
        raise RuntimeError("run_task() requires at least one non-LLM tool registered")

    return await react(goal, tools=tools, max_steps=max_steps, tool_name=tool_name)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_run_task.py -v`
Expected: 2 PASSED

**Step 5: Commit**

```bash
git add src/castor/lib/run_task.py tests/test_lib_run_task.py
git commit -m "feat: add run_task() Level 0 API to castor.lib"
```

---

### Task 8: Update `castor.lib.__init__` exports

**Files:**
- Modify: `src/castor/lib/__init__.py:1-13`

**Step 1: Write the failing test**

Add to `tests/test_lib_patterns.py`:

```python
def test_patterns_exported_from_lib():
    """All patterns should be importable from castor.lib."""
    from castor.lib import (
        conversation,
        map_reduce,
        parallel,
        plan_execute,
        react,
        run_task,
        supervisor,
    )

    assert callable(parallel)
    assert callable(react)
    assert callable(map_reduce)
    assert callable(plan_execute)
    assert callable(conversation)
    assert callable(supervisor)
    assert callable(run_task)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lib_patterns.py::test_patterns_exported_from_lib -v`
Expected: FAIL with "ImportError: cannot import name 'parallel' from 'castor.lib'"

**Step 3: Write minimal implementation**

Update `src/castor/lib/__init__.py`:

```python
"""castor.lib — standard library for agent developers."""

from castor.lib.patterns import (
    conversation,
    map_reduce,
    parallel,
    plan_execute,
    react,
    supervisor,
)
from castor.lib.primitives import budget, chat, tool, try_tool
from castor.lib.run_task import run_task
from castor.lib.spawn import join, spawn

__all__ = [
    "budget",
    "chat",
    "conversation",
    "join",
    "map_reduce",
    "parallel",
    "plan_execute",
    "react",
    "run_task",
    "spawn",
    "supervisor",
    "tool",
    "try_tool",
]
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_lib_patterns.py::test_patterns_exported_from_lib -v`
Expected: PASS

**Step 5: Run full test suite to verify no regressions**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS

**Step 6: Lint and format**

Run: `uv run ruff check src/ tests/ && uv run ruff format src/ tests/`
Expected: No errors

**Step 7: Commit**

```bash
git add src/castor/lib/__init__.py src/castor/lib/patterns.py tests/test_lib_patterns.py
git commit -m "feat: export all patterns from castor.lib"
```

---

### Task 9: CLI package — skeleton and `castor ps` / `castor inspect`

**Files:**
- Create: `src/castor/cli/__init__.py`
- Create: `src/castor/cli/process.py`
- Delete: `src/castor/cli.py` (old single-file CLI)
- Modify: `pyproject.toml:52-53` (entry point)
- Create: `tests/test_cli/__init__.py`
- Create: `tests/test_cli/test_process.py`

**Step 1: Write the failing test**

Create `tests/test_cli/__init__.py` (empty file).

Create `tests/test_cli/test_process.py`:

```python
"""Tests for castor.cli process commands — ps, inspect."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.persistence import CheckpointStore


@pytest.fixture()
def store(tmp_path):
    db_path = tmp_path / "test.db"
    return CheckpointStore(f"sqlite:///{db_path}")


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def saved_checkpoint(store, cap_mgr):
    cp = AgentCheckpoint(
        pid="agent-test-1234",
        status="COMPLETED",
        agent_function_name="test_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
        result="done",
    )
    store.save(cp)
    return cp


def test_cmd_ps(store, saved_checkpoint, capsys):
    from castor.cli.process import cmd_ps

    cmd_ps(store)
    output = capsys.readouterr().out
    assert "agent-test-1234" in output
    assert "DONE" in output


def test_cmd_ps_empty(store, capsys):
    from castor.cli.process import cmd_ps

    cmd_ps(store)
    output = capsys.readouterr().out
    assert "No checkpoints" in output or "No agents" in output


def test_cmd_inspect(store, saved_checkpoint, capsys):
    from castor.cli.process import cmd_inspect

    cmd_inspect(store, "agent-test-1234")
    output = capsys.readouterr().out
    assert "agent-test-1234" in output
    assert "COMPLETED" in output
    assert "test_agent" in output


def test_cmd_inspect_not_found(store):
    from castor.cli.process import cmd_inspect

    with pytest.raises(SystemExit):
        cmd_inspect(store, "nonexistent")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli/test_process.py::test_cmd_ps -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

Create `src/castor/cli/__init__.py`:

```python
"""Castor CLI — command-line interface for agent management."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="castor",
        description="Castor: secure microkernel for LLM agents",
    )
    parser.add_argument(
        "--db",
        default="castor.db",
        help="SQLite database path (default: castor.db)",
    )

    sub = parser.add_subparsers(dest="command")

    # Process commands
    sub.add_parser("ps", help="List agent processes")

    inspect_p = sub.add_parser("inspect", help="Inspect a checkpoint")
    inspect_p.add_argument("pid", help="Agent PID")

    # HITL commands
    approve_p = sub.add_parser("approve", help="Approve pending HITL")
    approve_p.add_argument("pid", help="Agent PID")

    reject_p = sub.add_parser("reject", help="Reject pending HITL")
    reject_p.add_argument("pid", help="Agent PID")
    reject_p.add_argument("--reason", required=True, help="Rejection reason")

    modify_p = sub.add_parser("modify", help="Modify pending HITL with feedback")
    modify_p.add_argument("pid", help="Agent PID")
    modify_p.add_argument("--feedback", required=True, help="Modification feedback")

    # Run command
    run_p = sub.add_parser("run", help="Run an agent")
    run_p.add_argument("agent", help="Agent module path (e.g. agent.py or agent.py:func)")
    run_p.add_argument("--budget", action="append", help="Budget as key=value (repeatable)")
    run_p.add_argument("--hitl", choices=["auto", "interactive"], default="auto", help="HITL policy")
    run_p.add_argument("--store", help="Checkpoint store URI (default: sqlite:///castor.db)")

    # Resume command
    resume_p = sub.add_parser("resume", help="Resume agent from checkpoint")
    resume_p.add_argument("pid", help="Agent PID")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    from castor.scheduler.persistence import CheckpointStore

    # Commands that need a store
    if args.command in ("ps", "inspect", "approve", "reject", "modify", "resume"):
        store = CheckpointStore(f"sqlite:///{args.db}")
        if args.command == "ps":
            from castor.cli.process import cmd_ps
            cmd_ps(store)
        elif args.command == "inspect":
            from castor.cli.process import cmd_inspect
            cmd_inspect(store, args.pid)
        elif args.command == "reject":
            from castor.cli.hitl import cmd_reject
            cmd_reject(store, args.pid, args.reason)
        elif args.command == "modify":
            from castor.cli.hitl import cmd_modify
            cmd_modify(store, args.pid, args.feedback)
        elif args.command == "approve":
            print("Error: approve requires runtime — use the host application.", file=sys.stderr)
            sys.exit(1)
        elif args.command == "resume":
            print("Error: resume not yet implemented.", file=sys.stderr)
            sys.exit(1)
    elif args.command == "run":
        from castor.cli.run import cmd_run
        cmd_run(args)


if __name__ == "__main__":
    main()
```

Create `src/castor/cli/process.py`:

```python
"""Process management commands: ps, inspect."""

from __future__ import annotations

import json
import sys

from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore

_STATUS_MARKERS = {
    "SUSPENDED_FOR_HITL": "HITL",
    "COMPLETED": "DONE",
    "RUNNING": "RUN ",
    "PREEMPTED": "PREM",
    "FAILED": "FAIL",
}


def cmd_ps(store: CheckpointStore) -> None:
    """List all agent processes with status."""
    pids = store.list_pids()
    if not pids:
        print("No agents found.")
        return

    for pid in pids:
        cp = store.load(pid)
        marker = _STATUS_MARKERS.get(cp.status, "??? ")
        line = f"  [{marker}] {pid}"
        if cp.pending_hitl:
            tool = cp.pending_hitl.get("tool_name", "?")
            line += f"  (pending: {tool})"
        print(line)


def cmd_inspect(store: CheckpointStore, pid: str) -> None:
    """Show detailed checkpoint information."""
    try:
        cp = store.load(pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"PID:    {cp.pid}")
    print(f"Status: {cp.status}")
    print(f"Agent:  {cp.agent_function_name}")
    if cp.parent_pid:
        print(f"Parent: {cp.parent_pid}")

    print("\nCapabilities:")
    for name, cap in cp.capabilities.items():
        remaining = cap.max_budget - cap.current_usage
        print(f"  {name}: {remaining:.1f} / {cap.max_budget:.1f} remaining")

    print(f"\nSyscall log: {len(cp.syscall_log)} entries")

    if cp.pending_hitl:
        print("\n--- Pending HITL ---")
        print(f"  Tool:      {cp.pending_hitl.get('tool_name')}")
        print(f"  Arguments: {json.dumps(cp.pending_hitl.get('arguments'), indent=4)}")
        if "child_pid" in cp.pending_hitl:
            print(f"  Child PID: {cp.pending_hitl['child_pid']}")

    if cp.result is not None:
        print(f"\nResult: {cp.result}")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli/test_process.py -v`
Expected: 4 PASSED

**Step 5: Delete old CLI file and update pyproject.toml**

Delete `src/castor/cli.py` and update `pyproject.toml` entry point:
```
[project.scripts]
castor = "castor.cli:main"
```
(The entry point string is the same — `castor.cli:main` — but now `castor.cli` resolves to the package `__init__.py` instead of the old single file.)

**Step 6: Commit**

```bash
git rm src/castor/cli.py
git add src/castor/cli/__init__.py src/castor/cli/process.py tests/test_cli/__init__.py tests/test_cli/test_process.py
git commit -m "feat: replace cli.py with cli/ package, add ps and inspect commands"
```

---

### Task 10: CLI — HITL commands (reject, modify)

**Files:**
- Create: `src/castor/cli/hitl.py`
- Create: `tests/test_cli/test_hitl.py`

**Step 1: Write the failing test**

Create `tests/test_cli/test_hitl.py`:

```python
"""Tests for castor.cli HITL commands — reject, modify."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.persistence import CheckpointStore


@pytest.fixture()
def store(tmp_path):
    db_path = tmp_path / "test.db"
    return CheckpointStore(f"sqlite:///{db_path}")


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def hitl_checkpoint(store, cap_mgr):
    cp = AgentCheckpoint(
        pid="agent-hitl-1",
        status="SUSPENDED_FOR_HITL",
        agent_function_name="test_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
        pending_hitl={"tool_name": "dangerous_tool", "arguments": {"x": 1}},
    )
    store.save(cp)
    return cp


def test_cmd_reject(store, hitl_checkpoint, capsys):
    from castor.cli.hitl import cmd_reject

    cmd_reject(store, "agent-hitl-1", "too dangerous")
    output = capsys.readouterr().out
    assert "Rejected" in output

    # Verify checkpoint was updated
    cp = store.load("agent-hitl-1")
    assert cp.pending_hitl is None


def test_cmd_reject_no_hitl(store, cap_mgr):
    cp = AgentCheckpoint(
        pid="agent-no-hitl",
        status="COMPLETED",
        agent_function_name="test",
        capabilities={},
    )
    store.save(cp)

    from castor.cli.hitl import cmd_reject

    with pytest.raises(SystemExit):
        cmd_reject(store, "agent-no-hitl", "reason")


def test_cmd_modify(store, hitl_checkpoint, capsys):
    from castor.cli.hitl import cmd_modify

    cmd_modify(store, "agent-hitl-1", "use safer params")
    output = capsys.readouterr().out
    assert "Modified" in output


def test_cmd_reject_not_found(store):
    from castor.cli.hitl import cmd_reject

    with pytest.raises(SystemExit):
        cmd_reject(store, "nonexistent", "reason")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli/test_hitl.py::test_cmd_reject -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

Create `src/castor/cli/hitl.py`:

```python
"""HITL commands: reject, modify."""

from __future__ import annotations

import sys

from castor.scheduler.hitl import HITLHandler
from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore


def cmd_reject(store: CheckpointStore, pid: str, reason: str) -> None:
    """Reject a pending HITL request."""
    try:
        cp = store.load(pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    if cp.pending_hitl is None:
        print(f"Error: checkpoint {pid!r} has no pending HITL.", file=sys.stderr)
        sys.exit(1)

    handler = HITLHandler()
    if handler.is_child_hitl(cp):
        print(
            f"Error: checkpoint {pid!r} has child HITL — "
            "use the host application's resume loop.",
            file=sys.stderr,
        )
        sys.exit(1)

    handler.reject(cp, reason)
    store.save(cp)
    print(f"Rejected: {pid}")


def cmd_modify(store: CheckpointStore, pid: str, feedback: str) -> None:
    """Modify a pending HITL request with feedback."""
    try:
        cp = store.load(pid)
    except CheckpointNotFoundError:
        print(f"Error: checkpoint {pid!r} not found.", file=sys.stderr)
        sys.exit(1)

    if cp.pending_hitl is None:
        print(f"Error: checkpoint {pid!r} has no pending HITL.", file=sys.stderr)
        sys.exit(1)

    handler = HITLHandler()
    if handler.is_child_hitl(cp):
        print(
            f"Error: checkpoint {pid!r} has child HITL — "
            "use the host application's resume loop.",
            file=sys.stderr,
        )
        sys.exit(1)

    handler.modify(cp, feedback)
    store.save(cp)
    print(f"Modified: {pid}")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli/test_hitl.py -v`
Expected: 4 PASSED

**Step 5: Commit**

```bash
git add src/castor/cli/hitl.py tests/test_cli/test_hitl.py
git commit -m "feat: add CLI HITL commands (reject, modify)"
```

---

### Task 11: CLI — `castor run` (agent loading + execution)

**Files:**
- Create: `src/castor/cli/run.py`
- Create: `tests/test_cli/test_run.py`

**Step 1: Write the failing test**

Create `tests/test_cli/test_run.py`:

```python
"""Tests for castor.cli.run — agent loading and execution."""

import textwrap

import pytest


def test_load_agent_convention(tmp_path):
    """Load agent function by convention (finds 'agent' or 'main')."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text(
        textwrap.dedent("""\
        async def agent():
            return "hello from agent"
        """)
    )

    from castor.cli.run import load_agent_function

    fn = load_agent_function(str(agent_file))
    assert fn.__name__ == "agent"


def test_load_agent_explicit_func(tmp_path):
    """Load agent function by explicit name (file:func)."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text(
        textwrap.dedent("""\
        async def my_custom_agent():
            return "custom"
        """)
    )

    from castor.cli.run import load_agent_function

    fn = load_agent_function(f"{agent_file}:my_custom_agent")
    assert fn.__name__ == "my_custom_agent"


def test_load_agent_main_fallback(tmp_path):
    """Falls back to 'main' if 'agent' not found."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text(
        textwrap.dedent("""\
        async def main():
            return "from main"
        """)
    )

    from castor.cli.run import load_agent_function

    fn = load_agent_function(str(agent_file))
    assert fn.__name__ == "main"


def test_load_agent_not_found(tmp_path):
    """Raises if no agent/main function found."""
    agent_file = tmp_path / "my_agent.py"
    agent_file.write_text("x = 1\n")

    from castor.cli.run import load_agent_function

    with pytest.raises(SystemExit):
        load_agent_function(str(agent_file))


def test_parse_budgets():
    """Parse --budget key=value pairs."""
    from castor.cli.run import parse_budgets

    result = parse_budgets(["api_usd=0.50", "tokens=1000"])
    assert result == {"api_usd": 0.50, "tokens": 1000.0}


def test_parse_budgets_none():
    from castor.cli.run import parse_budgets

    assert parse_budgets(None) is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli/test_run.py::test_load_agent_convention -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

Create `src/castor/cli/run.py`:

```python
"""Run command: load and execute agent functions."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def load_agent_function(agent_spec: str) -> Callable:
    """Load an agent function from a file path, optionally with :func_name.

    Resolution order for convention mode (no :func_name):
    1. Look for 'agent' function
    2. Look for 'main' function
    3. Fail with helpful error

    Args:
        agent_spec: Path like "agent.py" or "agent.py:my_func"
    """
    if ":" in agent_spec:
        file_path, func_name = agent_spec.rsplit(":", 1)
    else:
        file_path = agent_spec
        func_name = None

    path = Path(file_path).resolve()
    if not path.exists():
        print(f"Error: file {file_path!r} not found.", file=sys.stderr)
        sys.exit(1)

    # Load module from file path
    spec = importlib.util.spec_from_file_location("_castor_agent_module", path)
    if spec is None or spec.loader is None:
        print(f"Error: cannot load {file_path!r} as Python module.", file=sys.stderr)
        sys.exit(1)

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if func_name:
        fn = getattr(module, func_name, None)
        if fn is None:
            print(
                f"Error: function {func_name!r} not found in {file_path}.",
                file=sys.stderr,
            )
            sys.exit(1)
        return fn

    # Convention: try 'agent', then 'main'
    for name in ("agent", "main"):
        fn = getattr(module, name, None)
        if fn is not None and callable(fn):
            return fn

    print(
        f"Error: no 'agent' or 'main' function found in {file_path}. "
        f"Use {file_path}:function_name to specify explicitly.",
        file=sys.stderr,
    )
    sys.exit(1)


def parse_budgets(budget_args: list[str] | None) -> dict[str, float] | None:
    """Parse --budget key=value arguments into a dict."""
    if not budget_args:
        return None

    budgets: dict[str, float] = {}
    for item in budget_args:
        if "=" not in item:
            print(f"Error: invalid budget format {item!r}, expected key=value.", file=sys.stderr)
            sys.exit(1)
        key, value = item.split("=", 1)
        try:
            budgets[key] = float(value)
        except ValueError:
            print(f"Error: budget value {value!r} is not a number.", file=sys.stderr)
            sys.exit(1)
    return budgets


def cmd_run(args: argparse.Namespace) -> None:
    """Execute the 'castor run' command."""
    agent_fn = load_agent_function(args.agent)
    budgets = parse_budgets(args.budget)

    from castor.core import Castor

    store_uri = getattr(args, "store", None)
    kernel = Castor(
        store=store_uri,
        default_budgets=budgets,
    )

    if args.hitl == "interactive":
        from castor.scheduler.hitl import interactive

        cp = asyncio.run(
            kernel.run_until_complete(agent_fn, budgets=budgets, on_hitl=interactive)
        )
    else:
        cp = asyncio.run(kernel.run(agent_fn, budgets=budgets))

    print(f"\nPID:    {cp.pid}")
    print(f"Status: {cp.status}")
    if cp.result is not None:
        print(f"Result: {cp.result}")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli/test_run.py -v`
Expected: 6 PASSED

**Step 5: Commit**

```bash
git add src/castor/cli/run.py tests/test_cli/test_run.py
git commit -m "feat: add castor run command with agent loading"
```

---

### Task 12: Final integration — full test suite + lint

**Files:**
- All modified files from Tasks 1-11

**Step 1: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS (342 existing + ~20 new)

**Step 2: Lint and format**

Run: `uv run ruff check src/ tests/ --fix && uv run ruff format src/ tests/`
Expected: No errors after fix

**Step 3: Run full test suite again (post-format)**

Run: `uv run pytest tests/ -v`
Expected: All PASS

**Step 4: Final commit if any formatting changes**

```bash
git add -u
git commit -m "style: lint and format Phase B + C code"
```

**Step 5: Tag the release**

```bash
git tag v0.4-phase-bc-patterns-cli
```
