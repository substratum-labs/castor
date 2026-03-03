# Python Refinement Sprint — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden Castor's architecture (WAL, async spawn observability, tool timeouts), add full observability stack, polish for open-source, freeze API, and publish to TestPyPI as v0.1.0.

**Architecture:** Modify persistence layer (WAL table), proxy pipeline (timeout + WAL hooks), observability module (noop fallback pattern), and API surface markers. All changes are additive — no breaking changes to existing public API.

**Tech Stack:** Python 3.11+, Pydantic V2, SQLAlchemy, asyncio, OpenTelemetry (optional), Hypothesis (test dep)

**Design doc:** `docs/plans/2026-03-02-python-refinement-sprint.md`

---

## Week 1 — Architecture Hardening

### Task 1: WAL Table and Recovery in CheckpointStore

**Files:**
- Modify: `src/castor/stream/persistence.py`
- Test: `tests/test_persistence.py`

**Context:** Currently, if the kernel crashes between tool execution and `_append_record()`, the budget is deducted but the result is lost. We add a WAL table to track in-flight syscall execution.

**Step 1: Write failing tests for WAL**

Add to `tests/test_persistence.py`:

```python
class TestWAL:
    def test_write_wal_entry(self, store, cap_mgr):
        """WAL entry can be written and read back."""
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "hello"},
            budget_snapshot={"test": 99.0},
        )
        entries = store.list_pending_wal()
        assert len(entries) == 1
        assert entries[0]["pid"] == "test-001"
        assert entries[0]["status"] == "PENDING"

    def test_complete_wal_entry(self, store, cap_mgr):
        """Completing a WAL entry marks it COMPLETED with result."""
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "hello"},
            budget_snapshot={"test": 99.0},
        )
        store.complete_wal(pid="test-001", syscall_index=0, result=["found"])
        entries = store.list_pending_wal()
        assert len(entries) == 0

    def test_recover_refunds_pending_wal(self, store, cap_mgr):
        """Recovery refunds budget for PENDING WAL entries and marks ABANDONED."""
        checkpoint = make_checkpoint(cap_mgr)
        checkpoint.capabilities["test"].current_usage = 1.0  # was deducted before crash
        store.save(checkpoint)
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "hello"},
            budget_snapshot={"test": 0.0},  # usage before deduction
        )
        recovered = store.recover("test-001")
        assert recovered is not None
        # Budget should be refunded to pre-deduction snapshot
        assert recovered.capabilities["test"].current_usage == 0.0

    def test_recover_no_pending_returns_none(self, store, cap_mgr):
        """Recovery returns None when no PENDING WAL entries exist."""
        checkpoint = make_checkpoint(cap_mgr)
        store.save(checkpoint)
        assert store.recover("test-001") is None

    def test_gc_completed_wal(self, store, cap_mgr):
        """GC removes COMPLETED and ABANDONED WAL entries."""
        store.write_wal(
            pid="test-001",
            syscall_index=0,
            tool_name="search",
            arguments={"query": "a"},
            budget_snapshot={},
        )
        store.complete_wal(pid="test-001", syscall_index=0, result="ok")
        store.gc_wal()
        # Internal: verify table is clean (implementation detail, test via list)
        assert store.list_pending_wal() == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_persistence.py::TestWAL -v`
Expected: FAIL — `AttributeError: 'CheckpointStore' object has no attribute 'write_wal'`

**Step 3: Implement WAL in CheckpointStore**

Add to `src/castor/stream/persistence.py`:

```python
import json as _json

class WALRow(Base):
    __tablename__ = "wal_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pid = Column(String, nullable=False, index=True)
    syscall_index = Column(Integer, nullable=False)
    tool_name = Column(String, nullable=False)
    arguments = Column(Text, nullable=False)  # JSON
    budget_snapshot = Column(Text, nullable=False)  # JSON
    result = Column(Text, nullable=True)  # JSON, set on completion
    status = Column(String, nullable=False, default="PENDING")
    created_at = Column(DateTime, nullable=False)
```

Add these methods to `CheckpointStore`:

```python
def write_wal(
    self,
    pid: str,
    syscall_index: int,
    tool_name: str,
    arguments: dict,
    budget_snapshot: dict[str, float],
) -> None:
    """Write a PENDING WAL entry before tool execution."""
    with self._session_factory() as session:
        row = WALRow(
            pid=pid,
            syscall_index=syscall_index,
            tool_name=tool_name,
            arguments=_json.dumps(arguments),
            budget_snapshot=_json.dumps(budget_snapshot),
            status="PENDING",
            created_at=datetime.now(UTC),
        )
        session.add(row)
        session.commit()

def complete_wal(self, pid: str, syscall_index: int, result: Any) -> None:
    """Mark a WAL entry as COMPLETED after successful execution."""
    with self._session_factory() as session:
        row = (
            session.query(WALRow)
            .filter_by(pid=pid, syscall_index=syscall_index, status="PENDING")
            .first()
        )
        if row:
            row.status = "COMPLETED"
            row.result = _json.dumps(result)
            session.commit()

def list_pending_wal(self) -> list[dict]:
    """List all PENDING WAL entries."""
    with self._session_factory() as session:
        rows = session.query(WALRow).filter_by(status="PENDING").all()
        return [
            {
                "pid": r.pid,
                "syscall_index": r.syscall_index,
                "tool_name": r.tool_name,
                "arguments": _json.loads(r.arguments),
                "budget_snapshot": _json.loads(r.budget_snapshot),
                "status": r.status,
            }
            for r in rows
        ]

def recover(self, pid: str) -> AgentCheckpoint | None:
    """Recover from crash: refund PENDING WAL entries, return patched checkpoint."""
    pending = [e for e in self.list_pending_wal() if e["pid"] == pid]
    if not pending:
        return None
    checkpoint = self.load(pid)
    for entry in pending:
        snapshot = entry["budget_snapshot"]
        for resource, usage_before in snapshot.items():
            if resource in checkpoint.capabilities:
                checkpoint.capabilities[resource].current_usage = usage_before
    # Mark entries as ABANDONED
    with self._session_factory() as session:
        rows = (
            session.query(WALRow)
            .filter_by(pid=pid, status="PENDING")
            .all()
        )
        for row in rows:
            row.status = "ABANDONED"
        session.commit()
    self.save(checkpoint)
    return checkpoint

def gc_wal(self) -> None:
    """Remove COMPLETED and ABANDONED WAL entries."""
    with self._session_factory() as session:
        session.query(WALRow).filter(
            WALRow.status.in_(["COMPLETED", "ABANDONED"])
        ).delete(synchronize_session="fetch")
        session.commit()
```

Add `Integer` to the SQLAlchemy imports and `Any` to typing imports.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_persistence.py::TestWAL -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All 170+ tests pass (new WAL tests add ~5)

**Step 6: Lint**

Run: `uv run ruff check --fix src/castor/stream/persistence.py && uv run ruff format src/castor/stream/persistence.py`

**Step 7: Commit**

```bash
git add src/castor/stream/persistence.py tests/test_persistence.py
git commit -m "feat: add WAL table for crash recovery in CheckpointStore"
```

---

### Task 2: Wire WAL into SyscallProxy Pipeline

**Files:**
- Modify: `src/castor/stream/proxy.py`
- Test: `tests/test_proxy.py`

**Context:** The proxy needs to write WAL before executing tools and complete WAL after. The store is optional — if not provided, WAL is skipped (backwards compatible).

**Step 1: Write failing test**

Add to `tests/test_proxy.py`:

```python
from castor.stream.persistence import CheckpointStore


class TestWALIntegration:
    @pytest.fixture
    def store(self, tmp_path):
        return CheckpointStore(f"sqlite:///{tmp_path / 'test.db'}")

    async def test_wal_written_before_execution(self, registry, dam, cap_mgr, store):
        """WAL entry is written before tool executes."""
        register_search(registry)
        checkpoint = make_checkpoint(cap_mgr)
        store.save(checkpoint)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr, checkpoint_store=store)

        await proxy.syscall("search", {"query": "hello"})

        # WAL should be completed (no pending entries)
        assert store.list_pending_wal() == []

    async def test_wal_refund_on_failure(self, registry, dam, cap_mgr, store):
        """If tool execution fails, WAL stays PENDING for recovery."""
        @castor_tool(consumes="test", cost_per_use=2.0, registry=registry)
        async def failing_tool(query: str) -> str:
            raise RuntimeError("boom")

        checkpoint = make_checkpoint(cap_mgr)
        store.save(checkpoint)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr, checkpoint_store=store)

        with pytest.raises(RuntimeError, match="boom"):
            await proxy.syscall("failing_tool", {"query": "test"})

        # WAL entry left PENDING — recovery would refund
        pending = store.list_pending_wal()
        assert len(pending) == 1
        assert pending[0]["tool_name"] == "failing_tool"

    async def test_no_store_no_wal(self, registry, dam, cap_mgr):
        """When no store is provided, proxy works without WAL (backwards compat)."""
        register_search(registry)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)  # no store

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["result for hello"]
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_proxy.py::TestWALIntegration -v`
Expected: FAIL — `TypeError: SyscallProxy.__init__() got an unexpected keyword argument 'checkpoint_store'`

**Step 3: Add checkpoint_store parameter to SyscallProxy**

In `src/castor/stream/proxy.py`, add to `__init__`:

```python
def __init__(
    self,
    checkpoint: AgentCheckpoint,
    dam: CastorDam,
    capability_manager: CapabilityManager,
    lodge: CastorLodge | None = None,
    llm_tool_names: set[str] | None = None,
    kernel_tool_names: set[str] | None = None,
    agent_registry: AgentRegistry | None = None,
    checkpoint_store: CheckpointStore | None = None,  # NEW
) -> None:
    # ... existing ...
    self._store = checkpoint_store
```

Add `TYPE_CHECKING` import for `CheckpointStore`:
```python
if TYPE_CHECKING:
    from castor.lodge.core import CastorLodge
    from castor.stream.agent_registry import AgentRegistry
    from castor.stream.persistence import CheckpointStore
```

In the fast path (after budget deduction, before `await self._dam.execute()`), add WAL write:

```python
# ── WAL: log intent before execution ──
if self._store is not None:
    budget_snapshot = {
        tool_meta.consumes: self.checkpoint.capabilities[tool_meta.consumes].current_usage - tool_meta.cost_per_use
    }
    self._store.write_wal(
        pid=self.checkpoint.pid,
        syscall_index=len(self.checkpoint.syscall_log),
        tool_name=tool_name,
        arguments=validated,
        budget_snapshot=budget_snapshot,
    )

try:
    result = await self._dam.execute(tool_name, validated)
except BaseException:
    self._cap_mgr.refund(...)
    raise

# ── WAL: mark complete after execution ──
if self._store is not None:
    self._store.complete_wal(
        pid=self.checkpoint.pid,
        syscall_index=len(self.checkpoint.syscall_log),
        result=result,
    )

self._append_record(SyscallRecord(request=request, response=result))
return result
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_proxy.py -v`
Expected: All PASS

**Step 5: Run full suite + lint**

Run: `uv run pytest tests/ -v && uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/`

**Step 6: Commit**

```bash
git add src/castor/stream/proxy.py tests/test_proxy.py
git commit -m "feat: wire WAL into SyscallProxy fast path for crash recovery"
```

---

### Task 3: Async Spawn Observability — Persist Child Checkpoints at Spawn

**Files:**
- Modify: `src/castor/stream/proxy.py`
- Modify: `src/castor/stream/persistence.py` (add `list_by_parent` query)
- Test: `tests/test_spawn.py`

**Step 1: Write failing tests**

Add to `tests/test_persistence.py`:

```python
class TestParentPidQuery:
    def test_list_by_parent(self, store, cap_mgr):
        """List all checkpoints with a given parent_pid."""
        parent = make_checkpoint(cap_mgr, pid="parent-001")
        store.save(parent)

        child1 = AgentCheckpoint(
            pid="parent-001::child-0",
            parent_pid="parent-001",
            status="RUNNING",
            agent_function_name="child",
            capabilities=cap_mgr.create_capabilities({"test": 10.0}),
        )
        child2 = AgentCheckpoint(
            pid="parent-001::child-1",
            parent_pid="parent-001",
            status="COMPLETED",
            agent_function_name="child",
            capabilities=cap_mgr.create_capabilities({"test": 10.0}),
        )
        store.save(child1)
        store.save(child2)

        children = store.list_by_parent("parent-001")
        assert set(c.pid for c in children) == {
            "parent-001::child-0",
            "parent-001::child-1",
        }

    def test_list_by_parent_empty(self, store):
        assert store.list_by_parent("no-parent") == []
```

Add to `tests/test_spawn.py`:

```python
class TestAsyncSpawnPersistence:
    async def test_child_persisted_at_spawn(
        self, tool_registry, dam, cap_mgr, agent_registry, tmp_path
    ):
        """Child checkpoint is persisted to store immediately at async spawn."""
        from castor.stream.persistence import CheckpointStore

        store = CheckpointStore(f"sqlite:///{tmp_path / 'test.db'}")

        async def child_agent(proxy):
            return "child done"

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(cap_mgr)
        store.save(checkpoint)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)
        proxy._store = store  # inject store

        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "child_agent", "capabilities": {"test": 10.0}},
        )

        # Child should be in the store before join
        children = store.list_by_parent("parent-001")
        assert len(children) == 1
        assert children[0].pid == handle
        assert children[0].status == "RUNNING"

        # Clean up
        await proxy.syscall("join_agent", {"handle": handle})
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_persistence.py::TestParentPidQuery tests/test_spawn.py::TestAsyncSpawnPersistence -v`
Expected: FAIL

**Step 3: Implement**

In `src/castor/stream/persistence.py`, add `list_by_parent`:

```python
def list_by_parent(self, parent_pid: str) -> list[AgentCheckpoint]:
    """List all checkpoints with the given parent_pid."""
    with self._session_factory() as session:
        rows = session.query(CheckpointRow).all()
        results = []
        for row in rows:
            cp = AgentCheckpoint.model_validate_json(row.data)
            if cp.parent_pid == parent_pid:
                results.append(cp)
        return results
```

In `src/castor/stream/proxy.py` `_handle_spawn_async`, after creating `child_cp` and launching the task, add:

```python
# Persist child checkpoint for observability
if self._store is not None:
    self._store.save(child_cp)
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_persistence.py::TestParentPidQuery tests/test_spawn.py::TestAsyncSpawnPersistence -v`
Expected: All PASS

**Step 5: Add gc_orphans to CheckpointStore**

Test first:

```python
class TestGCOrphans:
    def test_gc_marks_orphaned_children(self, store, cap_mgr):
        """Children of completed parents with RUNNING status become ORPHANED."""
        parent = make_checkpoint(cap_mgr, pid="parent-001")
        parent.status = "COMPLETED"
        store.save(parent)

        child = AgentCheckpoint(
            pid="parent-001::child-0",
            parent_pid="parent-001",
            status="RUNNING",
            agent_function_name="child",
            capabilities=cap_mgr.create_capabilities({"test": 10.0}),
        )
        store.save(child)

        orphaned = store.gc_orphans()
        assert len(orphaned) == 1
        assert orphaned[0] == "parent-001::child-0"

        reloaded = store.load("parent-001::child-0")
        assert reloaded.status == "FAILED"  # mark as FAILED since ORPHANED isn't a status
```

Note: Since `AgentCheckpoint.status` is a `Literal` type, we'd need to add `"ORPHANED"` or reuse `"FAILED"`. To avoid changing the Pydantic model for now, mark orphans as `"FAILED"` with `preemption_reason="ORPHANED"`.

Implementation:

```python
def gc_orphans(self) -> list[str]:
    """Mark orphaned children (parent done, child still RUNNING) as FAILED."""
    orphaned: list[str] = []
    with self._session_factory() as session:
        all_rows = session.query(CheckpointRow).all()
        checkpoints = {
            r.pid: AgentCheckpoint.model_validate_json(r.data) for r in all_rows
        }
        for pid, cp in checkpoints.items():
            if cp.parent_pid and cp.status == "RUNNING":
                parent = checkpoints.get(cp.parent_pid)
                if parent and parent.status in ("COMPLETED", "FAILED"):
                    cp.status = "FAILED"
                    cp.preemption_reason = "ORPHANED"
                    self.save(cp)
                    orphaned.append(pid)
    return orphaned
```

**Step 6: Run full suite + lint + commit**

```bash
uv run pytest tests/ -v
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
git add src/castor/stream/persistence.py src/castor/stream/proxy.py tests/test_persistence.py tests/test_spawn.py
git commit -m "feat: persist async child checkpoints at spawn, add gc_orphans"
```

---

### Task 4: Unify _resume_child() with AgentRunner.run()

**Files:**
- Modify: `src/castor/stream/hitl.py`
- Modify: `src/castor/stream/runner.py`
- Test: `tests/test_spawn.py` (existing tests should keep passing)

**Context:** `HITLHandler._resume_child()` duplicates logic from `AgentRunner.run()`. We make `_resume_child()` delegate to `AgentRunner`.

**Step 1: Refactor `_resume_child` to use AgentRunner**

The key insight: `AgentRunner.run()` already:
- Creates a `SyscallProxy`
- Calls `agent_fn(proxy)`
- Handles `SuspendInterrupt` and `CancelledError`

Replace `_resume_child` body:

```python
async def _resume_child(
    self,
    parent_cp: AgentCheckpoint,
    child_cp: AgentCheckpoint,
    dam: CastorDam,
    capability_manager: CapabilityManager,
    agent_registry: AgentRegistry,
    lodge: CastorLodge | None = None,
) -> None:
    """Replay a child agent after its HITL was resolved."""
    from castor.stream.runner import AgentRunner

    agent_fn = agent_registry.get(child_cp.agent_function_name)
    runner = AgentRunner(dam, capability_manager, lodge=lodge, agent_registry=agent_registry)
    child_cp = await runner.run(agent_fn, child_cp)

    last = parent_cp.syscall_log[-1]
    last.child_checkpoint = child_cp

    if child_cp.status == "SUSPENDED_FOR_HITL":
        # Child suspended again — parent stays suspended
        return

    # Child completed — reclaim budget and update parent
    capability_manager.reclaim(parent_cp.capabilities, child_cp.capabilities)
    last.response = child_cp.result
    parent_cp.pending_hitl = None
    parent_cp.status = "RUNNING"
```

**Step 2: Run all spawn + HITL tests**

Run: `uv run pytest tests/test_spawn.py tests/test_hitl.py -v`
Expected: All PASS — behavior is unchanged

**Step 3: Run full suite**

Run: `uv run pytest tests/ -v`
Expected: All pass

**Step 4: Lint + commit**

```bash
uv run ruff check --fix src/castor/stream/hitl.py && uv run ruff format src/castor/stream/hitl.py
git add src/castor/stream/hitl.py
git commit -m "refactor: unify _resume_child with AgentRunner.run()"
```

---

### Task 5: Tool Execution Timeouts

**Files:**
- Modify: `src/castor/dam/registry.py` (add `timeout_seconds` field)
- Modify: `src/castor/dam/decorator.py` (accept timeout param)
- Modify: `src/castor/stream/proxy.py` (apply timeout during execution)
- Test: `tests/test_proxy.py`

**Step 1: Write failing tests**

Add to `tests/test_proxy.py`:

```python
class TestToolTimeout:
    async def test_async_tool_timeout(self, registry, dam, cap_mgr):
        """Async tool exceeding timeout raises asyncio.TimeoutError, budget refunded."""
        @castor_tool(consumes="test", cost_per_use=1.0, timeout_seconds=0.1, registry=registry)
        async def slow_tool(query: str) -> str:
            await asyncio.sleep(10)
            return "never reached"

        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        with pytest.raises(asyncio.TimeoutError):
            await proxy.syscall("slow_tool", {"query": "test"})

        # Budget refunded
        assert checkpoint.capabilities["test"].current_usage == 0.0

    async def test_sync_tool_timeout(self, registry, dam, cap_mgr):
        """Sync CPU-bound tool with timeout runs in executor and times out."""
        import time

        @castor_tool(consumes="test", cost_per_use=1.0, timeout_seconds=0.1, registry=registry)
        def cpu_bound_tool(query: str) -> str:
            time.sleep(10)
            return "never reached"

        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        with pytest.raises(asyncio.TimeoutError):
            await proxy.syscall("cpu_bound_tool", {"query": "test"})

        assert checkpoint.capabilities["test"].current_usage == 0.0

    async def test_no_timeout_default(self, registry, dam, cap_mgr):
        """Tools without timeout_seconds work normally (backwards compat)."""
        register_search(registry)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = SyscallProxy(checkpoint, dam, cap_mgr)

        result = await proxy.syscall("search", {"query": "hello"})
        assert result == ["result for hello"]
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_proxy.py::TestToolTimeout -v`
Expected: FAIL — `TypeError: castor_tool() got an unexpected keyword argument 'timeout_seconds'`

**Step 3: Add `timeout_seconds` to ToolMetadata**

In `src/castor/dam/registry.py`, add field:

```python
class ToolMetadata(BaseModel):
    # ... existing fields ...
    timeout_seconds: float | None = None
```

**Step 4: Add `timeout_seconds` to `@castor_tool` decorator**

In `src/castor/dam/decorator.py`:

```python
def castor_tool(
    consumes: str,
    cost_per_use: float = 1.0,
    requires_hitl: bool = False,
    destructive: bool = False,
    registry: ToolRegistry | None = None,
    timeout_seconds: float | None = None,  # NEW
) -> Callable:
    # ...
    def decorator(func: Callable) -> Callable:
        metadata = ToolMetadata(
            # ... existing ...
            timeout_seconds=timeout_seconds,  # NEW
        )
```

**Step 5: Apply timeout in SyscallProxy**

In `src/castor/stream/proxy.py`, replace the execution block in the fast path:

```python
# ── Fast Path execution with optional timeout ──
try:
    if tool_meta.timeout_seconds is not None:
        if tool_meta.is_async:
            result = await asyncio.wait_for(
                self._dam.execute(tool_name, validated),
                timeout=tool_meta.timeout_seconds,
            )
        else:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._dam.execute_sync(tool_name, validated)),
                timeout=tool_meta.timeout_seconds,
            )
    else:
        result = await self._dam.execute(tool_name, validated)
except BaseException:
    self._cap_mgr.refund(...)
    raise
```

Note: Need to add `execute_sync` to `CastorDam` for sync tool timeout support, or handle it differently. Simpler approach — check if tool_meta is async or sync within proxy:

```python
try:
    if tool_meta.timeout_seconds is not None:
        if tool_meta.is_async:
            result = await asyncio.wait_for(
                self._dam.execute(tool_name, validated),
                timeout=tool_meta.timeout_seconds,
            )
        else:
            loop = asyncio.get_running_loop()
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=1) as pool:
                result = await asyncio.wait_for(
                    loop.run_in_executor(pool, tool_meta.func, **validated),
                    timeout=tool_meta.timeout_seconds,
                )
    else:
        result = await self._dam.execute(tool_name, validated)
```

Actually, `ProcessPoolExecutor` can't pickle lambdas/closures. Use `ThreadPoolExecutor` for simplicity (still unblocks the event loop):

```python
from concurrent.futures import ThreadPoolExecutor

# In the fast path execution:
if tool_meta.timeout_seconds is not None and not tool_meta.is_async:
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = await asyncio.wait_for(
            loop.run_in_executor(pool, lambda: tool_meta.func(**validated)),
            timeout=tool_meta.timeout_seconds,
        )
elif tool_meta.timeout_seconds is not None:
    result = await asyncio.wait_for(
        self._dam.execute(tool_name, validated),
        timeout=tool_meta.timeout_seconds,
    )
else:
    result = await self._dam.execute(tool_name, validated)
```

Note: We use `tool_meta.func` directly for the sync-in-executor path rather than going through `dam.execute()` (which would `await` a sync function). The `is_async` flag on `ToolMetadata` already tells us which path to take. This is acceptable since validation already ran.

**Step 6: Run tests**

Run: `uv run pytest tests/test_proxy.py::TestToolTimeout -v`
Expected: All PASS

**Step 7: Full suite + lint + commit**

```bash
uv run pytest tests/ -v
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
git add src/castor/dam/registry.py src/castor/dam/decorator.py src/castor/stream/proxy.py tests/test_proxy.py
git commit -m "feat: add tool execution timeouts (ThreadPoolExecutor for sync tools)"
```

---

## Week 2 — Open-Source Polish + Observability

### Task 6: Fix README

**Files:**
- Modify: `README.md`

**Changes:**
1. Lodge status: `Planned` → `Complete`
2. Test count: `90 tests` → actual count (run `uv run pytest --co -q | tail -1` to get exact number)
3. Add badges after logo:
```markdown
[![CI](https://github.com/substrate-lab/castor/actions/workflows/ci.yml/badge.svg)](https://github.com/substrate-lab/castor/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
```
4. Add a shorter 15-line quickstart inline (keep existing detailed example)

**Commit:**
```bash
git add README.md
git commit -m "docs: update README — Lodge complete, accurate test count, badges"
```

---

### Task 7: Community Files

**Files:**
- Create: `CONTRIBUTING.md`
- Create: `CODE_OF_CONDUCT.md`
- Create: `.github/ISSUE_TEMPLATE/bug_report.md`
- Create: `.github/ISSUE_TEMPLATE/feature_request.md`
- Create: `.github/PULL_REQUEST_TEMPLATE.md`

**CONTRIBUTING.md content outline:**
- Prerequisites: Python 3.11+, uv
- Setup: `git clone`, `uv sync`
- Commands: `uv run pytest`, `uv run ruff check`, `uv run ruff format`
- Architecture overview: 4 subsystems, syscall proxy, checkpoint/replay
- PR expectations: tests pass, lint clean, describe what/why
- Code conventions: Pydantic models, agent function signatures, never bypass proxy

**CODE_OF_CONDUCT.md:** Contributor Covenant v2.1

**Bug report template:** Version, Python version, OS, steps to reproduce, expected/actual, logs

**Feature request template:** Use case, proposed solution, alternatives considered

**PR template:** Checklist — tests pass, lint clean, docs updated if needed, description of changes

**Commit:**
```bash
git add CONTRIBUTING.md CODE_OF_CONDUCT.md .github/
git commit -m "docs: add CONTRIBUTING, CODE_OF_CONDUCT, issue/PR templates"
```

---

### Task 8: Observability Module — Noop Fallback + Structured Logging

**Files:**
- Create: `src/castor/observability.py`
- Modify: `pyproject.toml` (add optional deps)
- Test: `tests/test_observability.py`

This is the largest Week 2 task. Build in two sub-steps: (a) noop module + logging, (b) wire into proxy.

**Step 1: Write tests for observability module**

```python
# tests/test_observability.py
"""Tests for the observability module (logging, tracing, metrics)."""

import logging

from castor.observability import get_logger, get_meter, get_tracer


class TestNoopFallback:
    def test_get_tracer_without_otel(self):
        """get_tracer returns a noop tracer when opentelemetry is not installed."""
        tracer = get_tracer("castor.test")
        # Should not raise — noop context manager
        with tracer.start_as_current_span("test_span"):
            pass

    def test_get_meter_without_otel(self):
        """get_meter returns a noop meter when opentelemetry is not installed."""
        meter = get_meter("castor.test")
        counter = meter.create_counter("test_counter")
        counter.add(1)  # Should not raise

    def test_get_logger(self):
        """get_logger returns a standard Python logger."""
        logger = get_logger("castor.test")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "castor.test"
```

**Step 2: Implement observability module**

```python
# src/castor/observability.py
"""Observability: structured logging, optional OpenTelemetry tracing and metrics.

Install extras for full observability:
    pip install castor[observability]

Without the extras, all tracing/metrics calls are noops with zero overhead.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any


def get_logger(name: str) -> logging.Logger:
    """Get a structured logger for the given module."""
    return logging.getLogger(name)


# ── OpenTelemetry tracing (optional) ──

try:
    from opentelemetry import trace

    def get_tracer(name: str) -> trace.Tracer:
        return trace.get_tracer(name)

except ImportError:

    class _NoopSpan:
        def set_attribute(self, key: str, value: Any) -> None:
            pass

        def set_status(self, status: Any) -> None:
            pass

        def record_exception(self, exception: BaseException) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class _NoopTracer:
        def start_as_current_span(self, name: str, **kwargs: Any) -> _NoopSpan:
            return _NoopSpan()

    def get_tracer(name: str) -> Any:
        return _NoopTracer()


# ── OpenTelemetry metrics (optional) ──

try:
    from opentelemetry import metrics

    def get_meter(name: str) -> metrics.Meter:
        return metrics.get_meter(name)

except ImportError:

    class _NoopCounter:
        def add(self, amount: float = 1, attributes: dict | None = None) -> None:
            pass

    class _NoopHistogram:
        def record(self, amount: float, attributes: dict | None = None) -> None:
            pass

    class _NoopGauge:
        def set(self, amount: float, attributes: dict | None = None) -> None:
            pass

    class _NoopMeter:
        def create_counter(self, name: str, **kwargs: Any) -> _NoopCounter:
            return _NoopCounter()

        def create_histogram(self, name: str, **kwargs: Any) -> _NoopHistogram:
            return _NoopHistogram()

        def create_up_down_counter(self, name: str, **kwargs: Any) -> _NoopCounter:
            return _NoopCounter()

    def get_meter(name: str) -> Any:
        return _NoopMeter()
```

**Step 3: Add optional deps to pyproject.toml**

```toml
[project.optional-dependencies]
observability = ["opentelemetry-api>=1.20", "opentelemetry-sdk>=1.20"]
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_observability.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/castor/observability.py tests/test_observability.py pyproject.toml
git commit -m "feat: add observability module with noop fallback for OTel"
```

---

### Task 9: Wire Observability into Proxy Pipeline

**Files:**
- Modify: `src/castor/stream/proxy.py`
- Modify: `src/castor/stream/runner.py`
- Modify: `src/castor/capability/manager.py`
- Test: `tests/test_observability.py` (extend)

**Context:** Add structured logging + tracing spans + metrics at key points in the syscall pipeline.

**Step 1: Add logging + spans to SyscallProxy**

At the top of `proxy.py`:

```python
import time
from castor.observability import get_logger, get_meter, get_tracer

logger = get_logger("castor.stream")
tracer = get_tracer("castor.stream")
meter = get_meter("castor.stream")

_syscall_counter = meter.create_counter("castor_syscalls_total")
_syscall_duration = meter.create_histogram("castor_syscall_duration_seconds")
_hitl_counter = meter.create_counter("castor_hitl_total")
_spawn_counter = meter.create_counter("castor_spawns_total")
```

In `syscall()` method, wrap the execution:

```python
async def syscall(self, tool_name: str, arguments: dict[str, Any]) -> Any:
    request = {"tool_name": tool_name, "arguments": arguments}
    start = time.perf_counter()

    # ... replay path ...
    if self._replay_index < len(self.checkpoint.syscall_log):
        # ... existing replay logic ...
        logger.debug("replay_hit", extra={"pid": self.checkpoint.pid, "tool": tool_name, "index": self._replay_index - 1})
        return record.response

    # ... after execution completes ...
    elapsed = time.perf_counter() - start
    _syscall_counter.add(1, {"tool": tool_name, "status": "success"})
    _syscall_duration.record(elapsed, {"tool": tool_name})
    logger.info("syscall_complete", extra={"pid": self.checkpoint.pid, "tool": tool_name, "latency_ms": elapsed * 1000})
```

For HITL suspend:
```python
if tool_meta.requires_hitl or tool_meta.destructive:
    _hitl_counter.add(1, {"action": "suspend"})
    logger.info("hitl_suspend", extra={"pid": self.checkpoint.pid, "tool": tool_name})
    # ... existing suspend logic ...
```

For spawns:
```python
_spawn_counter.add(1, {"type": "sync"})  # or "async"
logger.info("spawn", extra={"pid": self.checkpoint.pid, "child_pid": child_pid, "type": "sync"})
```

**Step 2: Add logging to CapabilityManager**

In `capability/manager.py`:

```python
from castor.observability import get_logger, get_meter

logger = get_logger("castor.capability")
meter = get_meter("castor.capability")
_budget_gauge = meter.create_up_down_counter("castor_budget_remaining")
```

In `deduct()`:
```python
logger.debug("budget_deduct", extra={"resource": resource_type, "cost": cost, "remaining": remaining - cost})
```

In `refund()`:
```python
logger.debug("budget_refund", extra={"resource": resource_type, "cost": cost})
```

**Step 3: Add logging to AgentRunner**

In `runner.py`:

```python
from castor.observability import get_logger

logger = get_logger("castor.stream")
```

In `run()`:
```python
logger.info("agent_start", extra={"pid": checkpoint.pid, "agent": checkpoint.agent_function_name})
# ... after completion ...
logger.info("agent_complete", extra={"pid": checkpoint.pid, "status": checkpoint.status})
```

**Step 4: Write integration test**

Add to `tests/test_observability.py`:

```python
class TestLoggingIntegration:
    async def test_syscall_emits_log(self, caplog):
        """Syscall execution emits structured log messages."""
        from castor.capability.manager import CapabilityManager
        from castor.dam.decorator import castor_tool
        from castor.dam.registry import ToolRegistry
        from castor.dam.validator import CastorDam
        from castor.models.checkpoint import AgentCheckpoint
        from castor.stream.proxy import SyscallProxy

        registry = ToolRegistry()

        @castor_tool(consumes="test", cost_per_use=1.0, registry=registry)
        def search(query: str) -> list:
            return [f"result for {query}"]

        dam = CastorDam(registry)
        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"test": 100.0})
        cp = AgentCheckpoint(
            pid="test-001",
            status="RUNNING",
            agent_function_name="test",
            capabilities=caps,
        )
        proxy = SyscallProxy(cp, dam, cap_mgr)

        with caplog.at_level(logging.DEBUG, logger="castor.stream"):
            await proxy.syscall("search", {"query": "hello"})

        assert any("syscall_complete" in r.message for r in caplog.records)
```

**Step 5: Run tests + lint + commit**

```bash
uv run pytest tests/test_observability.py tests/test_proxy.py -v
uv run pytest tests/ -v
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
git add src/castor/stream/proxy.py src/castor/stream/runner.py src/castor/capability/manager.py tests/test_observability.py
git commit -m "feat: wire structured logging + OTel tracing + metrics into kernel"
```

---

### Task 10: Quickstart Example

**Files:**
- Create: `examples/quickstart.py`

**Content:** A minimal self-contained example (see design doc for the code). Must be runnable:

```bash
cd examples && uv run python quickstart.py
```

**Commit:**
```bash
git add examples/quickstart.py
git commit -m "docs: add quickstart example for README"
```

---

## Week 3 — API Freeze + Hardening + Ship

### Task 11: Stable/Experimental Markers

**Files:**
- Create: `src/castor/api_status.py`
- Modify: `src/castor/__init__.py`
- Test: `tests/test_api_status.py`

**Step 1: Write test**

```python
# tests/test_api_status.py
from castor.api_status import experimental, stable


class TestAPIStatus:
    def test_stable_marker(self):
        @stable
        class MyClass:
            pass

        assert MyClass.__api_status__ == "stable"

    def test_experimental_marker(self):
        @experimental
        def my_func():
            pass

        assert my_func.__api_status__ == "experimental"

    def test_stable_exports(self):
        """Core stable APIs are marked."""
        from castor import SyscallProxy, AgentCheckpoint, CapabilityManager

        assert getattr(SyscallProxy, "__api_status__", None) == "stable"
        assert getattr(AgentCheckpoint, "__api_status__", None) == "stable"
        assert getattr(CapabilityManager, "__api_status__", None) == "stable"

    def test_experimental_exports(self):
        """Experimental APIs are marked."""
        from castor import CastorLodge, LLMSyscall

        assert getattr(CastorLodge, "__api_status__", None) == "experimental"
        assert getattr(LLMSyscall, "__api_status__", None) == "experimental"
```

**Step 2: Implement**

```python
# src/castor/api_status.py
"""API stability markers for Castor public interfaces.

@stable — Will not break between minor versions.
@experimental — May change in future versions.
"""

from typing import TypeVar

T = TypeVar("T")


def stable(obj: T) -> T:
    """Mark as stable public API."""
    obj.__api_status__ = "stable"  # type: ignore[attr-defined]
    return obj


def experimental(obj: T) -> T:
    """Mark as experimental — may change."""
    obj.__api_status__ = "experimental"  # type: ignore[attr-defined]
    return obj
```

Apply markers in `__init__.py` after imports:

```python
from castor.api_status import stable, experimental

# Apply stability markers
stable(SyscallProxy)
stable(AgentCheckpoint)
stable(SyscallRecord)
stable(Capability)
stable(SyscallRequest)
stable(SyscallResponse)
stable(CastorDam)
stable(CapabilityManager)
stable(HITLHandler)
stable(AgentRunner)
stable(CheckpointStore)
stable(castor_tool)
stable(SuspendInterrupt)
stable(CastorMessage)

experimental(CastorLodge)
experimental(LLMSyscall)
experimental(AgentRegistry)
experimental(castor_agent)
experimental(AgentNotFoundError)
```

**Step 3: Run tests + lint + commit**

```bash
uv run pytest tests/test_api_status.py -v
uv run pytest tests/ -v
uv run ruff check --fix src/ tests/ && uv run ruff format src/ tests/
git add src/castor/api_status.py src/castor/__init__.py tests/test_api_status.py
git commit -m "feat: add @stable/@experimental API markers"
```

---

### Task 12: Property-Based Tests (Hypothesis)

**Files:**
- Modify: `pyproject.toml` (add hypothesis to dev deps)
- Create: `tests/test_property_based.py`

**Step 1: Add hypothesis dependency**

In `pyproject.toml`:

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.8",
    "hypothesis>=6.0",
]
```

Run: `uv sync`

**Step 2: Write property-based tests**

```python
# tests/test_property_based.py
"""Property-based tests for Castor invariants using Hypothesis."""

import pytest
from hypothesis import given, settings, strategies as st

from castor.capability.manager import CapabilityManager
from castor.dam.decorator import castor_tool
from castor.dam.registry import ToolRegistry
from castor.dam.validator import CastorDam
from castor.models.capability import Capability
from castor.models.checkpoint import AgentCheckpoint, SyscallRecord
from castor.stream.proxy import SyscallProxy


# ── Strategies ──

budget_amount = st.floats(min_value=0.1, max_value=1000.0, allow_nan=False)
cost_amount = st.floats(min_value=0.1, max_value=10.0, allow_nan=False)


# ── Property 1: Budget Conservation ──

class TestBudgetConservation:
    @given(initial=budget_amount, cost=cost_amount)
    def test_deduct_refund_identity(self, initial, cost):
        """deduct then refund returns to original usage."""
        if cost > initial:
            return  # skip impossible cases
        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"res": initial})
        cap_mgr.deduct(caps, "res", cost)
        cap_mgr.refund(caps, "res", cost)
        assert abs(caps["res"].current_usage) < 1e-9

    @given(
        parent_budget=budget_amount,
        child_budget=st.floats(min_value=0.1, max_value=100.0, allow_nan=False),
        child_usage=st.floats(min_value=0.0, max_value=50.0, allow_nan=False),
    )
    def test_delegate_reclaim_conservation(self, parent_budget, child_budget, child_usage):
        """delegate + child usage + reclaim = parent deducted by child_usage only."""
        if child_budget > parent_budget or child_usage > child_budget:
            return
        cap_mgr = CapabilityManager()
        parent_caps = cap_mgr.create_capabilities({"res": parent_budget})
        child_caps = cap_mgr.delegate(parent_caps, {"res": child_budget})
        child_caps["res"].current_usage = child_usage
        cap_mgr.reclaim(parent_caps, child_caps)
        # Parent should have lost exactly child_usage
        assert abs(parent_caps["res"].current_usage - child_usage) < 1e-9


# ── Property 2: Replay Identity ──

class TestReplayIdentity:
    @given(num_syscalls=st.integers(min_value=1, max_value=20))
    @settings(max_examples=50)
    async def test_replay_produces_identical_results(self, num_syscalls):
        """Replaying N syscalls from cache returns identical values."""
        registry = ToolRegistry()

        @castor_tool(consumes="test", cost_per_use=0.0, registry=registry)
        def echo(value: str) -> str:
            return f"echo:{value}"

        dam = CastorDam(registry)
        cap_mgr = CapabilityManager()

        # Build a syscall log
        log = []
        for i in range(num_syscalls):
            log.append(
                SyscallRecord(
                    request={"tool_name": "echo", "arguments": {"value": f"msg-{i}"}},
                    response=f"echo:msg-{i}",
                )
            )

        # Replay from the log
        caps = cap_mgr.create_capabilities({"test": 1000.0})
        cp = AgentCheckpoint(
            pid="prop-test",
            status="RUNNING",
            agent_function_name="test",
            capabilities=caps,
            syscall_log=log,
        )
        proxy = SyscallProxy(cp, dam, cap_mgr)

        for i in range(num_syscalls):
            result = await proxy.syscall("echo", {"value": f"msg-{i}"})
            assert result == f"echo:msg-{i}"

        assert not proxy.is_replaying


# ── Property 3: HITL Modify Preserves Original ──

class TestHITLModifyInvariant:
    def test_modify_never_mutates_original_request(self):
        """HITL modify logs original request unmodified."""
        from castor.stream.hitl import HITLHandler

        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"test": 100.0})
        cp = AgentCheckpoint(
            pid="hitl-test",
            status="SUSPENDED_FOR_HITL",
            agent_function_name="test",
            capabilities=caps,
            pending_hitl={
                "tool_name": "delete_files",
                "arguments": {"paths": ["/original"]},
            },
        )
        original_request = cp.pending_hitl.copy()

        handler = HITLHandler()
        handler.modify(cp, "use /safe instead")

        logged = cp.syscall_log[-1]
        assert logged.request == original_request
        assert logged.response["status"] == "HITL_MODIFIED"
        assert logged.response["human_feedback"] == "use /safe instead"
```

**Step 3: Run tests**

Run: `uv run pytest tests/test_property_based.py -v`
Expected: All PASS

**Step 4: Lint + commit**

```bash
uv run ruff check --fix tests/test_property_based.py && uv run ruff format tests/test_property_based.py
git add pyproject.toml tests/test_property_based.py
git commit -m "test: add property-based tests for budget conservation, replay identity, HITL invariant"
```

---

### Task 13: Benchmark Baseline

**Files:**
- Create: `benchmarks/bench_baseline.py`

**Content:** Script that measures key performance metrics. Uses `time.perf_counter_ns`, runs 10k iterations, reports p50/p95/p99.

Metrics to benchmark:
1. Syscall fast path latency
2. Syscall replay path latency
3. Dam validation time
4. Checkpoint serialization (10, 100, 1000 syscalls)
5. Checkpoint persistence round-trip
6. Budget operations (deduct/refund cycle)
7. Lodge eviction (token counting + FIFO selection)

The script should be self-contained and produce a readable table to stdout.

**Run to verify:**
```bash
uv run python benchmarks/bench_baseline.py
```

**Commit:**
```bash
git add benchmarks/bench_baseline.py
git commit -m "perf: add baseline benchmark script for Rust comparison"
```

---

### Task 14: CHANGELOG + TestPyPI Publish

**Files:**
- Create: `CHANGELOG.md`
- Modify: `pyproject.toml` (verify metadata)

**Step 1: Write CHANGELOG.md**

```markdown
# Changelog

## v0.1.0 (2026-03-XX)

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
```

**Step 2: Verify pyproject.toml metadata**

Check: name, version, description, readme, license, authors, keywords, classifiers, URLs, optional-dependencies.

**Step 3: Build + test install**

```bash
uv build
# Inspect wheel
ls dist/
# Test install in a temporary venv
uv venv /tmp/castor-test && source /tmp/castor-test/bin/activate
pip install dist/castor-0.1.0-py3-none-any.whl
python -c "import castor; print(castor.__version__)"
deactivate
```

**Step 4: Publish to TestPyPI**

```bash
uv publish --publish-url https://test.pypi.org/legacy/
```

Verify:
```bash
pip install -i https://test.pypi.org/simple/ castor
```

**Step 5: Commit**

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "release: v0.1.0 — CHANGELOG, TestPyPI publish prep"
```

---

## Final Verification

After all 14 tasks:

```bash
uv run pytest tests/ -v         # All tests pass (170 + ~30 new ≈ 200)
uv run ruff check src/ tests/   # Zero lint errors
uv run ruff format --check src/ tests/  # All formatted
uv run python benchmarks/bench_baseline.py  # Benchmarks run
```

Tag the release:
```bash
git tag v0.1.0
```
