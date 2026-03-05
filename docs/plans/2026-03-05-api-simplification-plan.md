# API Simplification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `Castor` facade class and enhanced `SyscallProxy` calling conventions to reduce quickstart boilerplate from 10+ lines to 3 lines.

**Architecture:** New `src/castor/core.py` contains the `Castor` facade that internally assembles `ToolRegistry`, `CastorDam`, `CapabilityManager`, `AgentRunner`, and `HITLHandler`. `SyscallProxy` gains `__getattr__` (dynamic tool calls), `**kwargs` support on `syscall()`, and a `call()` method for function-reference style. All existing low-level APIs remain unchanged.

**Tech Stack:** Python 3.11+, Pydantic V2, pytest, pytest-asyncio

---

### Task 1: Add `**kwargs` support to `SyscallProxy.syscall()` (4B)

**Files:**
- Modify: `src/castor/stream/proxy.py:98` — `syscall` method signature
- Test: `tests/test_facade.py` (create new)

**Step 1: Write the failing test**

Create `tests/test_facade.py`:

```python
"""Tests for the Castor facade API and SyscallProxy enhancements."""

import pytest
import pytest_asyncio

from castor import (
    AgentCheckpoint,
    AgentRunner,
    CapabilityManager,
    CastorDam,
    SyscallProxy,
    castor_tool,
)
from castor.dam.registry import ToolRegistry


# ── Fixtures ──

@pytest.fixture()
def registry():
    reg = ToolRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
    async def search(query: str) -> list[str]:
        return [f"Result: {query}"]

    @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
    async def add(a: int, b: int) -> int:
        return a + b

    return reg


@pytest.fixture()
def dam(registry):
    return CastorDam(registry)


@pytest.fixture()
def cap_mgr():
    return CapabilityManager()


@pytest.fixture()
def checkpoint(cap_mgr):
    caps = cap_mgr.create_capabilities({"api": 100.0})
    return AgentCheckpoint(
        pid="test-001",
        status="RUNNING",
        agent_function_name="test_agent",
        capabilities=caps,
    )


@pytest.fixture()
def proxy(checkpoint, dam, cap_mgr):
    return SyscallProxy(checkpoint, dam, cap_mgr)


# ── Task 1: syscall kwargs ──


class TestSyscallKwargs:
    @pytest.mark.asyncio
    async def test_syscall_with_kwargs(self, proxy):
        """syscall() accepts keyword arguments instead of a dict."""
        result = await proxy.syscall("search", query="hello")
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_syscall_with_dict_still_works(self, proxy):
        """syscall() still accepts a dict (backward compat)."""
        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_syscall_kwargs_multiple_args(self, proxy):
        """syscall() kwargs works with multiple parameters."""
        result = await proxy.syscall("add", a=2, b=3)
        assert result == 5

    @pytest.mark.asyncio
    async def test_syscall_rejects_dict_and_kwargs(self, proxy):
        """syscall() raises if both positional dict and kwargs given."""
        with pytest.raises(TypeError, match="Cannot pass both"):
            await proxy.syscall("search", {"query": "hello"}, query="hello")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_facade.py::TestSyscallKwargs -v`
Expected: FAIL — `syscall()` doesn't accept kwargs yet

**Step 3: Implement kwargs support**

In `src/castor/stream/proxy.py`, change `syscall` signature from:

```python
async def syscall(self, tool_name: str, arguments: dict[str, Any]) -> Any:
```

to:

```python
async def syscall(self, tool_name: str, arguments: dict[str, Any] | None = None, /, **kwargs: Any) -> Any:
    """Main syscall entry point.

    Supports three calling styles:
        await proxy.syscall("search", {"query": "hello"})   # dict
        await proxy.syscall("search", query="hello")          # kwargs
    """
    if arguments is not None and kwargs:
        raise TypeError("Cannot pass both positional arguments dict and keyword arguments")
    if arguments is None:
        arguments = kwargs
```

The rest of the method body stays exactly the same — it already operates on the `arguments` dict.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_facade.py::TestSyscallKwargs -v`
Expected: PASS (4 tests)

**Step 5: Run full test suite to verify no regressions**

Run: `uv run pytest tests/ -x -q`
Expected: all existing tests pass (existing callers pass dicts, which still work)

**Step 6: Commit**

```bash
git add src/castor/stream/proxy.py tests/test_facade.py
git commit -m "feat: add kwargs support to SyscallProxy.syscall()"
```

---

### Task 2: Add `__getattr__` dynamic tool calls to `SyscallProxy` (4A)

**Files:**
- Modify: `src/castor/stream/proxy.py` — add `__getattr__` method
- Test: `tests/test_facade.py` — add `TestDynamicToolCalls`

**Step 1: Write the failing test**

Append to `tests/test_facade.py`:

```python
class TestDynamicToolCalls:
    @pytest.mark.asyncio
    async def test_proxy_dynamic_call(self, proxy):
        """proxy.search(query='hello') calls syscall('search', ...)."""
        result = await proxy.search(query="hello")
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_proxy_dynamic_multiple_args(self, proxy):
        """proxy.add(a=2, b=3) works with multiple params."""
        result = await proxy.add(a=2, b=3)
        assert result == 5

    @pytest.mark.asyncio
    async def test_proxy_dynamic_logs_syscall(self, proxy):
        """Dynamic calls go through syscall and appear in syscall_log."""
        await proxy.search(query="test")
        assert len(proxy.checkpoint.syscall_log) == 1
        record = proxy.checkpoint.syscall_log[0]
        assert record.request["tool_name"] == "search"
        assert record.request["arguments"] == {"query": "test"}

    def test_proxy_real_attrs_not_intercepted(self, proxy):
        """Real attributes like checkpoint, is_replaying are not intercepted."""
        _ = proxy.checkpoint  # should not raise
        _ = proxy.is_replaying  # should not raise

    def test_proxy_unknown_tool_raises(self, proxy):
        """Accessing a non-existent tool raises AttributeError."""
        with pytest.raises(AttributeError):
            proxy.nonexistent_tool_xyz
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_facade.py::TestDynamicToolCalls -v`
Expected: FAIL — no `__getattr__` yet

**Step 3: Implement `__getattr__`**

Add to `SyscallProxy` class in `src/castor/stream/proxy.py`, after the `_append_record` method:

```python
def __getattr__(self, name: str) -> Any:
    """Enable proxy.tool_name(...) style calls.

    Returns an async callable that delegates to syscall().
    Only triggers for names not found via normal attribute lookup.
    """
    # Check if it's a registered tool
    if self._dam.registry.has_tool(name):

        async def _tool_call(**kwargs: Any) -> Any:
            return await self.syscall(name, **kwargs)

        return _tool_call

    raise AttributeError(
        f"'{type(self).__name__}' has no attribute '{name}' "
        f"and '{name}' is not a registered tool"
    )
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_facade.py::TestDynamicToolCalls -v`
Expected: PASS (5 tests)

**Step 5: Run full suite**

Run: `uv run pytest tests/ -x -q`
Expected: all pass

**Step 6: Commit**

```bash
git add src/castor/stream/proxy.py tests/test_facade.py
git commit -m "feat: add __getattr__ dynamic tool calls to SyscallProxy"
```

---

### Task 3: Add `proxy.call(func, ...)` function-reference style (4C)

**Files:**
- Modify: `src/castor/stream/proxy.py` — add `call()` method
- Test: `tests/test_facade.py` — add `TestCallMethod`

**Step 1: Write the failing test**

Append to `tests/test_facade.py`:

```python
class TestCallMethod:
    @pytest.mark.asyncio
    async def test_call_with_function_ref(self, proxy, registry):
        """proxy.call(search, query='hello') uses function's tool name."""
        search_fn = registry.get("search").func
        result = await proxy.call(search_fn, query="hello")
        assert result == ["Result: hello"]

    @pytest.mark.asyncio
    async def test_call_logs_correctly(self, proxy, registry):
        """proxy.call() logs to syscall_log with correct tool name."""
        search_fn = registry.get("search").func
        await proxy.call(search_fn, query="test")
        assert proxy.checkpoint.syscall_log[0].request["tool_name"] == "search"

    @pytest.mark.asyncio
    async def test_call_without_metadata_raises(self, proxy):
        """proxy.call() raises if function has no _castor_metadata."""

        async def plain_func(x: int) -> int:
            return x

        with pytest.raises(TypeError, match="not a @castor_tool"):
            await proxy.call(plain_func, x=1)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_facade.py::TestCallMethod -v`
Expected: FAIL — no `call()` method yet

**Step 3: Implement `call()` method**

Add to `SyscallProxy` class in `src/castor/stream/proxy.py`:

```python
async def call(self, func: Any, /, **kwargs: Any) -> Any:
    """Call a tool by function reference.

    Usage: await proxy.call(search, query="hello")
    The function must be decorated with @castor_tool.
    """
    meta = getattr(func, "_castor_metadata", None)
    if meta is None:
        raise TypeError(
            f"{func!r} is not a @castor_tool — "
            f"only decorated functions can be used with proxy.call()"
        )
    return await self.syscall(meta.tool_name, **kwargs)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_facade.py::TestCallMethod -v`
Expected: PASS (3 tests)

**Step 5: Run full suite**

Run: `uv run pytest tests/ -x -q`
Expected: all pass

**Step 6: Commit**

```bash
git add src/castor/stream/proxy.py tests/test_facade.py
git commit -m "feat: add proxy.call(func, ...) function-reference calling style"
```

---

### Task 4: Implement `Castor` facade class

**Files:**
- Create: `src/castor/core.py`
- Test: `tests/test_facade.py` — add `TestCastorFacade`

**Step 1: Write the failing test**

Append to `tests/test_facade.py`:

```python
from castor.core import Castor


class TestCastorFacade:
    def test_create_with_default_registry(self):
        """Castor() picks up tools from default_registry."""
        from castor.dam.registry import default_registry

        # Register a tool on default_registry
        @castor_tool(consumes="api", cost_per_use=1.0)
        async def default_tool(x: int) -> int:
            return x * 2

        try:
            kernel = Castor()
            assert kernel._dam.registry.has_tool("default_tool")
        finally:
            # Clean up default registry
            default_registry._tools.pop("default_tool", None)

    def test_create_with_explicit_tools(self):
        """Castor(tools=[...]) uses only the given tools."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def explicit_tool(x: int) -> int:
            return x + 1

        kernel = Castor(tools=[explicit_tool])
        assert kernel._dam.registry.has_tool("explicit_tool")

    def test_create_with_custom_dam(self):
        """Castor(dam=...) uses the provided dam."""
        reg = ToolRegistry()
        dam = CastorDam(reg)
        kernel = Castor(dam=dam)
        assert kernel._dam is dam

    @pytest.mark.asyncio
    async def test_run_simple_agent(self):
        """kernel.run() creates checkpoint and runs agent."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def echo(msg: str) -> str:
            return f"echo: {msg}"

        kernel = Castor(tools=[echo])

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.syscall("echo", msg="hi")

        cp = await kernel.run(agent, budgets={"api": 10.0})
        assert cp.status == "COMPLETED"
        assert cp.result == "echo: hi"

    @pytest.mark.asyncio
    async def test_run_auto_generates_pid(self):
        """kernel.run() auto-generates a PID from function name."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def noop() -> str:
            return "ok"

        kernel = Castor(tools=[noop])

        async def my_agent(proxy: SyscallProxy) -> str:
            return "done"

        cp = await kernel.run(my_agent, budgets={"api": 10.0})
        assert cp.pid.startswith("my_agent-")

    @pytest.mark.asyncio
    async def test_run_with_explicit_pid(self):
        """kernel.run(pid=...) uses the given PID."""
        kernel = Castor(tools=[])

        async def agent(proxy: SyscallProxy) -> str:
            return "done"

        cp = await kernel.run(agent, pid="custom-pid")
        assert cp.pid == "custom-pid"

    @pytest.mark.asyncio
    async def test_run_without_budgets_is_unlimited(self):
        """kernel.run() without budgets allows unlimited tool calls."""
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def ping() -> str:
            return "pong"

        kernel = Castor(tools=[ping])

        async def agent(proxy: SyscallProxy) -> str:
            for _ in range(100):
                await proxy.syscall("ping")
            return "done"

        cp = await kernel.run(agent)
        assert cp.status == "COMPLETED"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_facade.py::TestCastorFacade -v`
Expected: FAIL — `castor.core` module doesn't exist

**Step 3: Implement `Castor` class**

Create `src/castor/core.py`:

```python
"""Castor: the unified kernel facade."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from castor.capability.manager import CapabilityManager
from castor.dam.registry import ToolRegistry, default_registry
from castor.dam.validator import CastorDam
from castor.models.checkpoint import AgentCheckpoint
from castor.stream.hitl import HITLHandler
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner


class Castor:
    """Unified kernel facade — assembles all subsystems behind a single object.

    Usage::

        kernel = Castor()
        cp = await kernel.run(my_agent, budgets={"api": 50.0})
    """

    def __init__(
        self,
        *,
        tools: list[Callable] | None = None,
        lodge: Any | None = None,
        agent_registry: Any | None = None,
        store: str | Any | None = None,
        dam: CastorDam | None = None,
        capability_manager: CapabilityManager | None = None,
    ) -> None:
        # ── Dam (tool validation + execution) ──
        if dam is not None:
            self._dam = dam
        elif tools is not None:
            registry = ToolRegistry()
            for func in tools:
                meta = getattr(func, "_castor_metadata", None)
                if meta is None:
                    raise TypeError(
                        f"{func!r} is not decorated with @castor_tool"
                    )
                registry.register(meta)
            self._dam = CastorDam(registry)
        else:
            self._dam = CastorDam(default_registry)

        # ── Capability Manager ──
        self._cap_mgr = capability_manager or CapabilityManager()

        # ── Optional subsystems ──
        self._lodge = lodge
        self._agent_registry = agent_registry
        self._hitl = HITLHandler()

        # ── Persistence ──
        self._store = None
        if store is not None:
            from castor.stream.persistence import CheckpointStore

            if isinstance(store, str):
                self._store = CheckpointStore(store)
            else:
                self._store = store

    async def run(
        self,
        agent_fn: Callable[[SyscallProxy], Any],
        *,
        budgets: dict[str, float] | None = None,
        checkpoint: AgentCheckpoint | None = None,
        pid: str | None = None,
    ) -> AgentCheckpoint:
        """Run an agent function.

        Args:
            agent_fn: The agent coroutine ``async def agent(proxy) -> result``.
            budgets: Resource budgets like ``{"api": 50.0}``.
                     Not provided = unlimited (no budget enforcement).
            checkpoint: Pass an existing checkpoint to resume (e.g. after HITL).
            pid: Custom process ID. Auto-generated if not provided.
        """
        if checkpoint is None:
            if budgets is not None:
                caps = self._cap_mgr.create_capabilities(budgets)
            else:
                caps = {}
            if pid is None:
                pid = f"{agent_fn.__name__}-{uuid.uuid4().hex[:8]}"
            checkpoint = AgentCheckpoint(
                pid=pid,
                status="RUNNING",
                agent_function_name=agent_fn.__name__,
                capabilities=caps,
            )

        runner = AgentRunner(
            self._dam,
            self._cap_mgr,
            lodge=self._lodge,
            agent_registry=self._agent_registry,
        )
        return await runner.run(agent_fn, checkpoint)

    async def approve(self, checkpoint: AgentCheckpoint) -> None:
        """Approve a pending HITL syscall."""
        if self._hitl.is_child_hitl(checkpoint):
            if self._agent_registry is None:
                raise RuntimeError(
                    "Child HITL approval requires an agent_registry on Castor"
                )
            await self._hitl.approve_child_hitl(
                checkpoint,
                self._dam,
                self._cap_mgr,
                self._agent_registry,
                lodge=self._lodge,
            )
        else:
            await self._hitl.approve(checkpoint, self._dam, self._cap_mgr)

    def reject(self, checkpoint: AgentCheckpoint, reason: str) -> None:
        """Reject a pending HITL syscall with feedback."""
        if self._hitl.is_child_hitl(checkpoint):
            raise NotImplementedError(
                "Child HITL rejection requires runtime — use HITLHandler directly"
            )
        self._hitl.reject(checkpoint, reason)

    def modify(self, checkpoint: AgentCheckpoint, feedback: str) -> None:
        """Approve with modification — log feedback for LLM re-planning."""
        if self._hitl.is_child_hitl(checkpoint):
            raise NotImplementedError(
                "Child HITL modification requires runtime — use HITLHandler directly"
            )
        self._hitl.modify(checkpoint, feedback)

    async def save(self, checkpoint: AgentCheckpoint) -> None:
        """Persist checkpoint to the configured store."""
        if self._store is None:
            raise RuntimeError("No store configured — pass store= to Castor()")
        self._store.save(checkpoint)

    def load(self, pid: str) -> AgentCheckpoint:
        """Load a checkpoint from the configured store."""
        if self._store is None:
            raise RuntimeError("No store configured — pass store= to Castor()")
        return self._store.load(pid)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_facade.py::TestCastorFacade -v`
Expected: PASS (7 tests)

**Step 5: Run full suite**

Run: `uv run pytest tests/ -x -q`
Expected: all pass

**Step 6: Commit**

```bash
git add src/castor/core.py tests/test_facade.py
git commit -m "feat: add Castor facade class with run/approve/reject/modify"
```

---

### Task 5: Add HITL facade tests

**Files:**
- Test: `tests/test_facade.py` — add `TestCastorHITL`

**Step 1: Write the failing test**

Append to `tests/test_facade.py`:

```python
class TestCastorHITL:
    @pytest.fixture()
    def hitl_kernel(self):
        reg = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=reg)
        async def safe_tool() -> str:
            return "safe"

        @castor_tool(
            consumes="api", cost_per_use=1.0,
            destructive=True, requires_hitl=True, registry=reg,
        )
        async def dangerous_tool(target: str) -> str:
            return f"destroyed {target}"

        return Castor(tools=[safe_tool, dangerous_tool])

    @pytest.mark.asyncio
    async def test_approve_flow(self, hitl_kernel):
        async def agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("safe_tool")
            result = await proxy.syscall("dangerous_tool", target="test")
            return f"done: {result}"

        cp = await hitl_kernel.run(agent, budgets={"api": 10.0})
        assert cp.status == "SUSPENDED_FOR_HITL"
        assert cp.pending_hitl["tool_name"] == "dangerous_tool"

        await hitl_kernel.approve(cp)
        cp = await hitl_kernel.run(agent, checkpoint=cp)
        assert cp.status == "COMPLETED"
        assert cp.result == "done: destroyed test"

    @pytest.mark.asyncio
    async def test_reject_flow(self, hitl_kernel):
        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.syscall("dangerous_tool", target="prod")
            if isinstance(result, dict) and result.get("status") == "HITL_REJECTED":
                return "aborted"
            return f"done: {result}"

        cp = await hitl_kernel.run(agent, budgets={"api": 10.0})
        assert cp.status == "SUSPENDED_FOR_HITL"

        hitl_kernel.reject(cp, reason="too risky")
        cp = await hitl_kernel.run(agent, checkpoint=cp)
        assert cp.status == "COMPLETED"
        assert cp.result == "aborted"

    @pytest.mark.asyncio
    async def test_modify_flow(self, hitl_kernel):
        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.syscall("dangerous_tool", target="prod")
            if isinstance(result, dict) and result.get("status") == "HITL_MODIFIED":
                return f"modified: {result['human_feedback']}"
            return f"done: {result}"

        cp = await hitl_kernel.run(agent, budgets={"api": 10.0})
        assert cp.status == "SUSPENDED_FOR_HITL"

        hitl_kernel.modify(cp, feedback="use staging instead")
        cp = await hitl_kernel.run(agent, checkpoint=cp)
        assert cp.status == "COMPLETED"
        assert "use staging instead" in cp.result
```

**Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_facade.py::TestCastorHITL -v`
Expected: PASS (3 tests) — these should pass with the Task 4 implementation

**Step 3: Commit**

```bash
git add tests/test_facade.py
git commit -m "test: add HITL facade tests for approve/reject/modify"
```

---

### Task 6: Export `Castor` from `__init__.py`

**Files:**
- Modify: `src/castor/__init__.py` — add Castor import and export

**Step 1: Write the failing test**

Append to `tests/test_facade.py`:

```python
class TestPublicExports:
    def test_import_castor_from_top_level(self):
        """Castor is importable from top-level package."""
        from castor import Castor

        assert Castor is not None

    def test_minimal_import_set(self):
        """Quickstart only needs 3 imports."""
        from castor import Castor, SyscallProxy, castor_tool

        assert all(x is not None for x in [Castor, SyscallProxy, castor_tool])
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_facade.py::TestPublicExports -v`
Expected: FAIL — `Castor` not in `castor.__init__`

**Step 3: Add export**

In `src/castor/__init__.py`, add:

After the existing imports (line ~11), add:
```python
from castor.core import Castor
```

Add `stable(Castor)` after the existing `stable()` calls.

Add `"Castor"` to `__all__`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_facade.py::TestPublicExports -v`
Expected: PASS (2 tests)

**Step 5: Run full suite**

Run: `uv run pytest tests/ -x -q`
Expected: all pass

**Step 6: Lint**

Run: `uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/`
Expected: clean

**Step 7: Commit**

```bash
git add src/castor/__init__.py tests/test_facade.py
git commit -m "feat: export Castor from top-level package"
```

---

### Task 7: Update README quickstart

**Files:**
- Modify: `README.md` — rewrite Quick Start section

**Step 1: Rewrite Quick Start**

Replace the Quick Start code block in `README.md` with the new facade API:

```python
from castor import Castor, castor_tool, SyscallProxy

@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for: {query}"]

kernel = Castor()

async def my_agent(proxy: SyscallProxy) -> str:
    results = await proxy.search(query="hello")
    return f"Found: {results}"

# result = asyncio.run(kernel.run(my_agent, budgets={"api": 50.0}))
```

Also update the detailed HITL example in the `<details>` block to use the facade API.

**Step 2: Update `examples/quickstart.py`**

Rewrite to use the facade API (Castor class, kernel.run, kernel.approve).

**Step 3: Verify examples run**

Run: `uv run python examples/quickstart.py`
Expected: runs successfully with same output behavior

**Step 4: Commit**

```bash
git add README.md examples/quickstart.py
git commit -m "docs: rewrite quickstart to use Castor facade API"
```

---

### Task 8: Final verification

**Step 1: Full test suite**

Run: `uv run pytest tests/ -v`
Expected: all tests pass (existing + new facade tests)

**Step 2: Lint clean**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: clean

**Step 3: Verify all examples still work**

Run:
```bash
uv run python examples/quickstart.py
uv run python examples/01_checkpoint_replay.py
uv run python examples/02_hitl_feedback.py
```
Expected: all run without errors (old examples use low-level APIs which are unchanged)
