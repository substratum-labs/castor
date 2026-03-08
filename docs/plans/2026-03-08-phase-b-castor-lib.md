# Phase B: castor.lib Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement `castor.lib` — the standard library layer that lets agent functions use `tool()`, `chat()`, etc. without receiving a `proxy` parameter.

**Architecture:** ContextVar bridge holds the current SyscallProxy. `Castor.run()` / `AgentRunner.run()` sets it before invoking the agent. Library functions (`tool`, `chat`, `budget`, `spawn`, `join`, `try_tool`) read it implicitly. Legacy agents (with `proxy` param) still work — signature auto-detection.

**Tech Stack:** Python 3.11+ contextvars, asyncio, pytest-asyncio, Pydantic V2

**Design doc:** `docs/plans/2026-03-08-phase-b-castor-lib-design.md`

---

### Task 1: ContextVar Bridge (`_context.py`)

**Files:**
- Create: `src/castor/lib/__init__.py`
- Create: `src/castor/lib/_context.py`
- Test: `tests/test_lib_context.py`

**Step 1: Write the failing tests**

```python
"""Tests for castor.lib._context — ContextVar bridge."""

import pytest

from castor.lib._context import get_proxy, set_proxy


def test_get_proxy_outside_run_raises():
    """get_proxy() raises RuntimeError when no proxy is set."""
    with pytest.raises(RuntimeError, match="castor.lib functions must be called inside"):
        get_proxy()


def test_set_and_get_proxy(gate, cap_mgr):
    """set_proxy() makes the proxy available via get_proxy()."""
    from castor.models.checkpoint import AgentCheckpoint
    from castor.scheduler.proxy import SyscallProxy

    cp = AgentCheckpoint(
        pid="test-ctx-1", status="RUNNING", agent_function_name="test"
    )
    proxy = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(proxy)
    assert get_proxy() is proxy
```

Fixtures `gate` and `cap_mgr` follow the existing pattern in the test suite — create a `ToolRegistry`, register a tool, build `SyscallGate(registry)` and `CapabilityManager()`. Use `conftest.py` if not already present, or inline.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lib_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'castor.lib'`

**Step 3: Implement `_context.py` and `__init__.py`**

`src/castor/lib/__init__.py`:
```python
"""castor.lib — standard library for agent functions."""
```

`src/castor/lib/_context.py`:
```python
"""ContextVar bridge: implicit SyscallProxy access for castor.lib functions."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from castor.scheduler.proxy import SyscallProxy

_proxy_var: ContextVar[SyscallProxy] = ContextVar("castor_proxy")


def get_proxy() -> SyscallProxy:
    """Return the current SyscallProxy.

    Raises RuntimeError if called outside ``Castor.run()``.
    """
    try:
        return _proxy_var.get()
    except LookupError:
        raise RuntimeError(
            "castor.lib functions must be called inside Castor.run()"
        ) from None


def set_proxy(proxy: SyscallProxy) -> None:
    """Set the current SyscallProxy (called by AgentRunner)."""
    _proxy_var.set(proxy)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lib_context.py -v`
Expected: 2 passed

**Step 5: Commit**

```bash
git add src/castor/lib/ tests/test_lib_context.py
git commit -m "feat(lib): add ContextVar bridge (_context.py)"
```

---

### Task 2: Primitives (`primitives.py`)

**Files:**
- Create: `src/castor/lib/primitives.py`
- Modify: `src/castor/lib/__init__.py` (add re-exports)
- Test: `tests/test_lib_primitives.py`

**Step 1: Write the failing tests**

```python
"""Tests for castor.lib.primitives — tool, chat, budget, try_tool."""

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

    reg.register(search._castor_metadata)
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
        pid="test-prim-1",
        status="RUNNING",
        agent_function_name="test",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    p = SyscallProxy(cp, gate, cap_mgr)
    set_proxy(p)
    return p


@pytest.mark.asyncio()
async def test_tool(proxy):
    from castor.lib import tool

    result = await tool("search", query="hello")
    assert result == "results for hello"


@pytest.mark.asyncio()
async def test_try_tool(proxy):
    from castor.lib import try_tool

    result = await try_tool("search", query="hello")
    assert result == "results for hello"


def test_budget(proxy):
    from castor.lib import budget

    remaining = budget("api")
    assert remaining == 10.0


@pytest.mark.asyncio()
async def test_chat_calls_tool(proxy, registry, gate):
    """chat() delegates to the named LLM tool."""
    @castor_tool(consumes="api", cost_per_use=1.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        return f"LLM says: {prompt}"

    registry.register(llm_inference._castor_metadata)

    from castor.lib import chat

    result = await chat("summarize this")
    assert result == "LLM says: summarize this"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lib_primitives.py -v`
Expected: FAIL — `ImportError: cannot import name 'tool' from 'castor.lib'`

**Step 3: Implement `primitives.py` and update `__init__.py`**

`src/castor/lib/primitives.py`:
```python
"""Core primitives: tool, chat, budget, try_tool."""

from __future__ import annotations

from typing import Any

from castor.lib._context import get_proxy


async def tool(name: str, /, **kwargs: Any) -> Any:
    """Call a registered tool by name."""
    return await get_proxy().syscall(name, **kwargs)


async def chat(
    prompt: str,
    *,
    system: str = "",
    tool_name: str = "llm_inference",
) -> str:
    """Call an LLM tool."""
    return await get_proxy().syscall(tool_name, prompt=prompt, system=system)


def budget(resource: str) -> float:
    """Return remaining budget for a resource type."""
    return get_proxy().budget(resource)


async def try_tool(name: str, /, **kwargs: Any) -> Any:
    """Call a tool — semantic alias communicating that failure is expected."""
    return await get_proxy().syscall(name, **kwargs)
```

Update `src/castor/lib/__init__.py`:
```python
"""castor.lib — standard library for agent functions."""

from castor.lib.primitives import budget, chat, tool, try_tool

__all__ = ["budget", "chat", "tool", "try_tool"]
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lib_primitives.py -v`
Expected: 4 passed

**Step 5: Commit**

```bash
git add src/castor/lib/ tests/test_lib_primitives.py
git commit -m "feat(lib): add primitives — tool, chat, budget, try_tool"
```

---

### Task 3: Spawn primitives (`spawn.py`)

**Files:**
- Create: `src/castor/lib/spawn.py`
- Modify: `src/castor/lib/__init__.py` (add spawn, join)
- Test: `tests/test_lib_spawn.py`

**Step 1: Write the failing tests**

```python
"""Tests for castor.lib.spawn — spawn and join."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.lib._context import set_proxy
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.agent_registry import AgentRegistry, castor_agent
from castor.scheduler.proxy import SyscallProxy


@pytest.fixture()
def registry():
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    reg.register(search._castor_metadata)
    return reg


@pytest.fixture()
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def agent_reg():
    return AgentRegistry()


@pytest.fixture()
def proxy(gate, cap_mgr, agent_reg):
    cp = AgentCheckpoint(
        pid="test-spawn-1",
        status="RUNNING",
        agent_function_name="parent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    p = SyscallProxy(
        cp, gate, cap_mgr, agent_registry=agent_reg
    )
    set_proxy(p)
    return p


@pytest.mark.asyncio()
async def test_spawn_and_join(proxy, agent_reg):
    from castor.lib import join, spawn

    @castor_agent(registry=agent_reg)
    async def child_agent(p: SyscallProxy) -> str:
        return "child done"

    handle = await spawn("child_agent", capabilities={"api": 2.0})
    assert isinstance(handle, str)
    result = await join(handle)
    assert result == "child done"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lib_spawn.py -v`
Expected: FAIL — `ImportError: cannot import name 'spawn' from 'castor.lib'`

**Step 3: Implement `spawn.py` and update `__init__.py`**

`src/castor/lib/spawn.py`:
```python
"""Spawn primitives: spawn, join."""

from __future__ import annotations

from typing import Any

from castor.lib._context import get_proxy


async def spawn(
    agent_name: str, *, capabilities: dict[str, float] | None = None
) -> str:
    """Spawn a child agent asynchronously, return a join handle."""
    return await get_proxy().spawn(agent_name, capabilities=capabilities)


async def join(handle: str) -> Any:
    """Wait for a spawned child agent to complete and return its result."""
    return await get_proxy().join(handle)
```

Update `src/castor/lib/__init__.py`:
```python
"""castor.lib — standard library for agent functions."""

from castor.lib.primitives import budget, chat, tool, try_tool
from castor.lib.spawn import join, spawn

__all__ = ["budget", "chat", "join", "spawn", "tool", "try_tool"]
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lib_spawn.py -v`
Expected: 1 passed

**Step 5: Commit**

```bash
git add src/castor/lib/ tests/test_lib_spawn.py
git commit -m "feat(lib): add spawn and join primitives"
```

---

### Task 4: Signature detection + ContextVar injection in `AgentRunner.run()`

**Files:**
- Modify: `src/castor/scheduler/runner.py:52-82` (the `run()` method)
- Test: `tests/test_lib_signature.py`

**Step 1: Write the failing tests**

```python
"""Tests for dual-signature detection in AgentRunner / Castor.run()."""

import pytest

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.runner import AgentRunner


@pytest.fixture()
def registry():
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    reg.register(search._castor_metadata)
    return reg


@pytest.fixture()
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def runner(gate, cap_mgr):
    return AgentRunner(gate, cap_mgr)


@pytest.mark.asyncio()
async def test_legacy_agent_with_proxy_param(runner, cap_mgr):
    """Legacy agent (1 param) still works."""
    async def my_agent(proxy):
        result = await proxy.syscall("search", query="test")
        return result

    cp = AgentCheckpoint(
        pid="sig-legacy",
        status="RUNNING",
        agent_function_name="my_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    cp = await runner.run(my_agent, cp)
    assert cp.status == "COMPLETED"
    assert cp.result == "results for test"


@pytest.mark.asyncio()
async def test_new_style_agent_no_params(runner, cap_mgr):
    """New-style agent (0 params) uses castor.lib via ContextVar."""
    from castor.lib import tool

    async def my_agent():
        return await tool("search", query="test")

    cp = AgentCheckpoint(
        pid="sig-new",
        status="RUNNING",
        agent_function_name="my_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    cp = await runner.run(my_agent, cp)
    assert cp.status == "COMPLETED"
    assert cp.result == "results for test"


@pytest.mark.asyncio()
async def test_contextvar_set_for_legacy_agent(runner, cap_mgr):
    """ContextVar is set even for legacy agents — enables gradual migration."""
    from castor.lib import budget

    async def my_agent(proxy):
        remaining = budget("api")
        return remaining

    cp = AgentCheckpoint(
        pid="sig-mixed",
        status="RUNNING",
        agent_function_name="my_agent",
        capabilities=cap_mgr.create_capabilities({"api": 10.0}),
    )
    cp = await runner.run(my_agent, cp)
    assert cp.status == "COMPLETED"
    assert cp.result == 10.0
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lib_signature.py -v`
Expected: `test_new_style_agent_no_params` FAILS (RuntimeError: castor.lib functions must be called inside Castor.run), `test_contextvar_set_for_legacy_agent` FAILS (same), `test_legacy_agent_with_proxy_param` PASSES

**Step 3: Modify `AgentRunner.run()` in `src/castor/scheduler/runner.py`**

Add import at top of file:
```python
import inspect
```

Replace lines 80-82 (after proxy creation, the try block):
```python
        # Set ContextVar so castor.lib functions work (for both new and legacy agents)
        from castor.lib._context import set_proxy
        set_proxy(proxy)

        # Detect agent signature: 0 required params = new-style, 1+ = legacy
        sig = inspect.signature(agent_fn)
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]

        try:
            if len(required) == 0:
                checkpoint.result = await agent_fn()
            else:
                checkpoint.result = await agent_fn(proxy)
```

Rest of the try/except block stays the same.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lib_signature.py -v`
Expected: 3 passed

Also run the full suite to confirm no regressions:
Run: `uv run pytest tests/ -v`
Expected: 332 passed (329 existing + 3 new)

**Step 5: Commit**

```bash
git add src/castor/scheduler/runner.py tests/test_lib_signature.py
git commit -m "feat(lib): signature detection + ContextVar injection in AgentRunner"
```

---

### Task 5: End-to-end integration test

**Files:**
- Test: `tests/test_lib_integration.py`

**Step 1: Write the integration test**

```python
"""End-to-end integration: new-style agents with Castor facade."""

import pytest

from castor import Castor, castor_tool
from castor.lib import budget, chat, tool


@pytest.fixture()
def search_tool():
    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"found: {query}"
    return search


@pytest.fixture()
def llm_tool():
    @castor_tool(consumes="api", cost_per_use=2.0)
    def llm_inference(prompt: str, system: str = "") -> str:
        return f"LLM: {prompt}"
    return llm_inference


@pytest.mark.asyncio()
async def test_new_style_agent_e2e(search_tool, llm_tool):
    """Full pipeline: Castor() → new-style agent → castor.lib calls."""
    kernel = Castor(tools=[search_tool, llm_tool])

    async def my_agent():
        result = await tool("search", query="hello")
        summary = await chat(f"summarize: {result}")
        remaining = budget("api")
        return {"result": result, "summary": summary, "budget": remaining}

    cp = await kernel.run(my_agent, budgets={"api": 10.0})
    assert cp.status == "COMPLETED"
    assert cp.result["result"] == "found: hello"
    assert cp.result["summary"] == "LLM: summarize: found: hello"
    assert cp.result["budget"] == 7.0  # 10 - 1 (search) - 2 (llm)


@pytest.mark.asyncio()
async def test_legacy_agent_still_works(search_tool):
    """Existing legacy agents are not broken."""
    kernel = Castor(tools=[search_tool])

    async def legacy_agent(proxy):
        return await proxy.syscall("search", query="legacy")

    cp = await kernel.run(legacy_agent, budgets={"api": 10.0})
    assert cp.status == "COMPLETED"
    assert cp.result == "found: legacy"


@pytest.mark.asyncio()
async def test_mixed_agent_legacy_with_lib(search_tool):
    """Legacy agent can also use castor.lib (gradual migration)."""
    kernel = Castor(tools=[search_tool])

    async def mixed_agent(proxy):
        # Use proxy directly
        r1 = await proxy.syscall("search", query="via-proxy")
        # Also use castor.lib
        r2 = await tool("search", query="via-lib")
        return [r1, r2]

    cp = await kernel.run(mixed_agent, budgets={"api": 10.0})
    assert cp.status == "COMPLETED"
    assert cp.result == ["found: via-proxy", "found: via-lib"]
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_lib_integration.py -v`
Expected: 3 passed

**Step 3: Run full test suite + lint**

Run: `uv run pytest tests/ -v`
Expected: 335+ passed, 0 failed

Run: `uv run ruff check src/ tests/`
Expected: All checks passed

Run: `uv run ruff format --check src/ tests/`
Expected: All files formatted

**Step 4: Commit**

```bash
git add tests/test_lib_integration.py
git commit -m "test(lib): add end-to-end integration tests for castor.lib"
```

---

### Task 6: Final validation, exports, and cleanup

**Files:**
- Verify: all new files lint clean
- Verify: `from castor.lib import tool, chat, budget, spawn, join, try_tool` works
- No changes to `src/castor/__init__.py` (by design — operator/agent separation)

**Step 1: Smoke-check imports**

```bash
uv run python -c "from castor.lib import tool, chat, budget, spawn, join, try_tool; print('OK')"
```

**Step 2: Full test suite**

Run: `uv run pytest tests/ -v`
Expected: All passed

**Step 3: Lint and format**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: Clean

**Step 4: Commit any cleanup, tag**

```bash
git tag v0.3-phase-b-lib-core
```
