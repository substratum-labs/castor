"""Tests for sub-agent spawning: AgentRegistry, SyscallProxy spawn, child HITL."""

import pytest

from castor.budget.manager import BudgetManager, InsufficientBudgetError
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import (
    AgentCheckpoint,
    SuspendInterrupt,
    SyscallRecord,
)
from castor.scheduler.agent_registry import (
    AgentNotFoundError,
    AgentRegistry,
    castor_agent,
)
from castor.scheduler.hitl import HITLHandler
from castor.scheduler.proxy import SyscallProxy
from castor.scheduler.runner import AgentRunner

# ── Fixtures ──


@pytest.fixture
def tool_registry():
    return ToolRegistry()


@pytest.fixture
def gate(tool_registry):
    return SyscallGate(tool_registry)


@pytest.fixture
def budget_mgr():
    return BudgetManager()


@pytest.fixture
def agent_registry():
    return AgentRegistry()


@pytest.fixture
def handler():
    return HITLHandler()


def register_search(tool_registry):
    @castor_tool(consumes="test", cost_per_use=1.0, registry=tool_registry)
    def search(query: str) -> list:
        return [f"result for {query}"]

    return search


def register_delete(tool_registry):
    @castor_tool(
        consumes="test",
        cost_per_use=1.0,
        destructive=True,
        requires_hitl=True,
        registry=tool_registry,
    )
    def delete_files(paths: list[str]) -> int:
        return len(paths)

    return delete_files


def make_checkpoint(budget_mgr, caps=None, syscall_log=None):
    if caps is None:
        caps = budget_mgr.create_budgets({"test": 100.0})
    return AgentCheckpoint(
        pid="parent-001",
        status="RUNNING",
        agent_function_name="parent_agent",
        capabilities=caps,
        syscall_log=syscall_log or [],
    )


def make_proxy(checkpoint, gate, budget_mgr, agent_registry=None, lodge=None):
    return SyscallProxy(
        checkpoint=checkpoint,
        gate=gate,
        capability_manager=budget_mgr,
        lodge=lodge,
        agent_registry=agent_registry,
    )


# ── AgentRegistry tests ──


class TestAgentRegistry:
    def test_register_and_get(self, agent_registry):
        async def my_agent(proxy):
            return "done"

        agent_registry.register("my_agent", my_agent)
        assert agent_registry.get("my_agent") is my_agent

    def test_get_unknown_raises(self, agent_registry):
        with pytest.raises(AgentNotFoundError, match="not_registered"):
            agent_registry.get("not_registered")

    def test_has_agent(self, agent_registry):
        async def my_agent(proxy):
            return "done"

        agent_registry.register("my_agent", my_agent)
        assert agent_registry.has_agent("my_agent") is True
        assert agent_registry.has_agent("other") is False

    def test_list_agents(self, agent_registry):
        async def a(proxy):
            return "a"

        async def b(proxy):
            return "b"

        agent_registry.register("beta", b)
        agent_registry.register("alpha", a)
        assert agent_registry.list_agents() == ["alpha", "beta"]


class TestCastorAgentDecorator:
    def test_decorator_registers_with_explicit_name(self):
        reg = AgentRegistry()

        @castor_agent(name="researcher", registry=reg)
        async def researcher_agent(proxy):
            return "research"

        assert reg.has_agent("researcher")
        assert reg.get("researcher") is researcher_agent

    def test_decorator_uses_function_name(self):
        reg = AgentRegistry()

        @castor_agent(registry=reg)
        async def summarizer(proxy):
            return "summary"

        assert reg.has_agent("summarizer")

    def test_decorator_uses_default_registry(self):
        from castor.scheduler.agent_registry import default_agent_registry

        @castor_agent(name="auto_registered")
        async def auto_agent(proxy):
            return "auto"

        assert default_agent_registry.has_agent("auto_registered")
        # Clean up to avoid polluting other tests
        default_agent_registry._agents.pop("auto_registered", None)


# ── Spawn via SyscallProxy ──


class TestSpawnHappyPath:
    async def test_spawn_child_completes(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Child agent runs, returns result, budget reclaimed."""
        register_search(tool_registry)

        async def child_agent(proxy):
            r = await proxy.syscall("search", {"query": "child"})
            return {"answer": r}

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        result = await proxy.syscall(
            "spawn_agent",
            {"agent_name": "child_agent", "capabilities": {"test": 10.0}},
        )

        assert result == {"answer": ["result for child"]}
        assert len(checkpoint.syscall_log) == 1
        record = checkpoint.syscall_log[0]
        assert record.request["tool_name"] == "spawn_agent"
        assert record.child_checkpoint is not None
        assert record.child_checkpoint.status == "COMPLETED"
        assert record.child_checkpoint.result == {"answer": ["result for child"]}

    async def test_spawn_budget_delegation(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Parent budget is deducted by delegation, child uses some, rest reclaimed."""
        register_search(tool_registry)

        async def child_agent(proxy):
            await proxy.syscall("search", {"query": "x"})
            return "done"

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        # Parent starts with 100 test budget
        await proxy.syscall(
            "spawn_agent",
            {"agent_name": "child_agent", "capabilities": {"test": 20.0}},
        )

        # Child used 1.0 of 20.0, so 19.0 reclaimed to parent
        # Parent usage: 20 delegated - 19 reclaimed = 1.0 net
        assert checkpoint.capabilities["test"].current_usage == 1.0

    async def test_spawn_child_pid_deterministic(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Child PID follows parent_pid::agent_name-N pattern."""

        async def noop_agent(proxy):
            return "ok"

        agent_registry.register("worker", noop_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        await proxy.syscall(
            "spawn_agent",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )

        child_cp = checkpoint.syscall_log[0].child_checkpoint
        assert child_cp.pid == "parent-001::worker-0"
        assert child_cp.parent_pid == "parent-001"

    async def test_spawn_multiple_children(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Spawning two children yields incrementing PIDs."""

        async def noop_agent(proxy):
            return "ok"

        agent_registry.register("worker", noop_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        await proxy.syscall(
            "spawn_agent",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )
        await proxy.syscall(
            "spawn_agent",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )

        assert checkpoint.syscall_log[0].child_checkpoint.pid == "parent-001::worker-0"
        assert checkpoint.syscall_log[1].child_checkpoint.pid == "parent-001::worker-1"


class TestSpawnErrors:
    async def test_spawn_no_registry_raises(self, gate, budget_mgr):
        """spawn_agent without AgentRegistry raises RuntimeError."""
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry=None)

        with pytest.raises(RuntimeError, match="AgentRegistry"):
            await proxy.syscall(
                "spawn_agent",
                {"agent_name": "x", "capabilities": {}},
            )

    async def test_spawn_unknown_agent_raises(self, gate, budget_mgr, agent_registry):
        """spawn_agent with unregistered name raises AgentNotFoundError."""
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(AgentNotFoundError, match="ghost"):
            await proxy.syscall(
                "spawn_agent",
                {"agent_name": "ghost", "capabilities": {"test": 1.0}},
            )

    async def test_spawn_insufficient_budget_raises(
        self, gate, budget_mgr, agent_registry
    ):
        """Requesting more budget than parent has raises InsufficientBudgetError."""

        async def noop(proxy):
            return "ok"

        agent_registry.register("noop", noop)
        caps = budget_mgr.create_budgets({"test": 5.0})
        checkpoint = make_checkpoint(budget_mgr, caps=caps)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(InsufficientBudgetError):
            await proxy.syscall(
                "spawn_agent",
                {"agent_name": "noop", "capabilities": {"test": 999.0}},
            )

    async def test_spawn_child_exception_reclaims_budget(
        self, gate, budget_mgr, agent_registry
    ):
        """If child raises unexpected exception, delegated budget is reclaimed."""

        async def crashing_child(proxy):
            raise RuntimeError("child exploded")

        agent_registry.register("crashing_child", crashing_child)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(RuntimeError, match="child exploded"):
            await proxy.syscall(
                "spawn_agent",
                {"agent_name": "crashing_child", "capabilities": {"test": 20.0}},
            )

        # Budget fully reclaimed — no leak
        assert checkpoint.capabilities["test"].current_usage == 0.0


# ── Child HITL suspension and propagation ──


class TestChildHITLSuspension:
    async def test_child_hitl_suspends_parent(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """When child hits HITL, parent also suspends."""
        register_delete(tool_registry)

        async def dangerous_child(proxy):
            await proxy.syscall("delete_files", {"paths": ["/important"]})
            return "deleted"

        agent_registry.register("dangerous_child", dangerous_child)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "dangerous_child",
                    "capabilities": {"test": 10.0},
                },
            )

        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert checkpoint.pending_hitl is not None
        assert checkpoint.pending_hitl["tool_name"] == "spawn_agent"
        assert checkpoint.pending_hitl["child_pid"].startswith("parent-001::")

        # Child checkpoint is saved in last syscall record
        last_record = checkpoint.syscall_log[-1]
        assert last_record.child_checkpoint is not None
        child_cp = last_record.child_checkpoint
        assert child_cp.status == "SUSPENDED_FOR_HITL"
        assert child_cp.pending_hitl["tool_name"] == "delete_files"


class TestChildHITLHandler:
    async def test_is_child_hitl(self, handler, budget_mgr):
        checkpoint = make_checkpoint(budget_mgr)
        assert handler.is_child_hitl(checkpoint) is False

        checkpoint.pending_hitl = {
            "tool_name": "spawn_agent",
            "arguments": {"agent_name": "x"},
        }
        assert handler.is_child_hitl(checkpoint) is True

    async def test_approve_child_hitl(
        self, tool_registry, gate, budget_mgr, agent_registry, handler
    ):
        """Approving child HITL executes the child's blocked tool and resumes."""
        register_delete(tool_registry)

        async def dangerous_child(proxy):
            result = await proxy.syscall("delete_files", {"paths": ["/a"]})
            return {"deleted": result}

        agent_registry.register("dangerous_child", dangerous_child)

        # First: spawn and get suspended
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "dangerous_child",
                    "capabilities": {"test": 10.0},
                },
            )

        # Now approve the child HITL
        await handler.approve_child_hitl(checkpoint, gate, budget_mgr, agent_registry)

        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None

        # Last record should have child's result
        last = checkpoint.syscall_log[-1]
        assert last.child_checkpoint.status == "COMPLETED"
        assert last.response == {"deleted": 1}

    async def test_reject_child_hitl(
        self, tool_registry, gate, budget_mgr, agent_registry, handler
    ):
        """Rejecting child HITL logs feedback and resumes child (which completes)."""
        register_delete(tool_registry)

        async def child_with_fallback(proxy):
            result = await proxy.syscall("delete_files", {"paths": ["/a"]})
            # After HITL_REJECTED, the LLM would see the rejection and return
            return {"result": result}

        agent_registry.register("child_with_fallback", child_with_fallback)

        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "child_with_fallback",
                    "capabilities": {"test": 10.0},
                },
            )

        await handler.reject_child_hitl(
            checkpoint, "Too dangerous", gate, budget_mgr, agent_registry
        )

        assert checkpoint.status == "RUNNING"
        # Child should have the rejection in its log
        child_cp = checkpoint.syscall_log[-1].child_checkpoint
        rejection_record = child_cp.syscall_log[0]
        assert rejection_record.response["status"] == "HITL_REJECTED"

    async def test_modify_child_hitl(
        self, tool_registry, gate, budget_mgr, agent_registry, handler
    ):
        """Modifying child HITL logs modification feedback and resumes."""
        register_delete(tool_registry)

        async def child_agent(proxy):
            result = await proxy.syscall("delete_files", {"paths": ["/a"]})
            return {"result": result}

        agent_registry.register("child_agent", child_agent)

        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "child_agent",
                    "capabilities": {"test": 10.0},
                },
            )

        await handler.modify_child_hitl(
            checkpoint, "Only delete temp files", gate, budget_mgr, agent_registry
        )

        assert checkpoint.status == "RUNNING"
        child_cp = checkpoint.syscall_log[-1].child_checkpoint
        mod_record = child_cp.syscall_log[0]
        assert mod_record.response["status"] == "HITL_MODIFIED"
        assert "temp files" in mod_record.response["human_feedback"]

    async def test_approve_child_hitl_child_crashes(
        self, tool_registry, gate, budget_mgr, agent_registry, handler
    ):
        """Child crash after HITL approval unblocks parent with FAILED child."""
        register_delete(tool_registry)

        async def crashing_after_hitl(proxy):
            await proxy.syscall("delete_files", {"paths": ["/a"]})
            raise RuntimeError("child crashed after HITL approval")

        agent_registry.register("crashing_after_hitl", crashing_after_hitl)

        # Spawn child → child hits HITL → parent suspends
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "crashing_after_hitl",
                    "capabilities": {"test": 10.0},
                },
            )

        initial_usage = checkpoint.capabilities["test"].current_usage

        # Approve child HITL — child resumes, then crashes
        await handler.approve_child_hitl(checkpoint, gate, budget_mgr, agent_registry)

        # Parent unblocked
        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None

        # Child marked FAILED
        last = checkpoint.syscall_log[-1]
        assert last.child_checkpoint.status == "FAILED"
        assert last.response is None

        # Budget reclaimed (child's unused budget returned)
        assert checkpoint.capabilities["test"].current_usage < initial_usage

    async def test_async_approve_child_hitl_child_crashes(
        self, tool_registry, gate, budget_mgr, agent_registry, handler
    ):
        """If async child crashes after HITL approval at join, parent is unblocked."""
        register_delete(tool_registry)

        async def crashing_after_hitl(proxy):
            await proxy.syscall("delete_files", {"paths": ["/a"]})
            raise RuntimeError("async child crashed after HITL approval")

        agent_registry.register("crashing_after_hitl", crashing_after_hitl)

        # Async spawn → join → child hits HITL → parent suspends
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {
                "agent_name": "crashing_after_hitl",
                "capabilities": {"test": 10.0},
            },
        )

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall("join_agent", {"handle": handle})

        initial_usage = checkpoint.capabilities["test"].current_usage

        # Approve child HITL — child resumes, then crashes
        await handler.approve_child_hitl(checkpoint, gate, budget_mgr, agent_registry)

        # Parent unblocked
        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None

        # Child marked FAILED
        last = checkpoint.syscall_log[-1]
        assert last.child_checkpoint.status == "FAILED"
        assert last.response is None

        # Budget reclaimed
        assert checkpoint.capabilities["test"].current_usage < initial_usage

    async def test_approve_child_no_spawn_raises(
        self, gate, budget_mgr, agent_registry, handler
    ):
        """approve_child_hitl raises if pending_hitl is not a spawn."""
        checkpoint = make_checkpoint(budget_mgr)
        checkpoint.pending_hitl = {
            "tool_name": "delete_files",
            "arguments": {"paths": ["/a"]},
        }
        with pytest.raises(ValueError, match="use approve"):
            await handler.approve_child_hitl(
                checkpoint, gate, budget_mgr, agent_registry
            )


# ── AgentRunner with AgentRegistry ──


class TestAgentRunnerWithRegistry:
    async def test_runner_passes_registry_to_proxy(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """AgentRunner wires agent_registry into SyscallProxy for spawn support."""
        register_search(tool_registry)
        spawned = []

        async def child_agent(proxy):
            r = await proxy.syscall("search", {"query": "from-child"})
            return r

        agent_registry.register("child_agent", child_agent)

        async def parent_agent(proxy):
            result = await proxy.syscall(
                "spawn_agent",
                {"agent_name": "child_agent", "capabilities": {"test": 10.0}},
            )
            spawned.append(result)
            return "parent done"

        runner = AgentRunner(gate, budget_mgr, agent_registry=agent_registry)
        checkpoint = make_checkpoint(budget_mgr)
        checkpoint.agent_function_name = "parent_agent"

        result = await runner.run(parent_agent, checkpoint)

        assert result.status == "COMPLETED"
        assert result.result == "parent done"
        assert spawned == [["result for from-child"]]


# ── Spawn replay ──


class TestSpawnReplay:
    async def test_spawn_result_replayed_from_cache(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """On resume, a completed spawn replays from syscall_log cache."""

        async def child_agent(proxy):
            return "should not run"

        agent_registry.register("child_agent", child_agent)

        # Pre-populate syscall_log with a cached spawn result
        child_cp = AgentCheckpoint(
            pid="parent-001::child_agent-0",
            parent_pid="parent-001",
            status="COMPLETED",
            agent_function_name="child_agent",
            capabilities=budget_mgr.create_budgets({"test": 10.0}),
            result="cached child result",
        )
        checkpoint = make_checkpoint(
            budget_mgr,
            syscall_log=[
                SyscallRecord(
                    request={
                        "tool_name": "spawn_agent",
                        "arguments": {
                            "agent_name": "child_agent",
                            "capabilities": {"test": 10.0},
                        },
                    },
                    response="cached child result",
                    child_checkpoint=child_cp,
                )
            ],
        )
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        result = await proxy.syscall(
            "spawn_agent",
            {"agent_name": "child_agent", "capabilities": {"test": 10.0}},
        )

        assert result == "cached child result"
        assert proxy._replay_index == 1
        assert not proxy.is_replaying


# ── Async spawn/join ──


class TestSpawnAsyncHappyPath:
    async def test_spawn_async_returns_handle(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """spawn_agent_async returns a child PID handle immediately."""

        async def child_agent(proxy):
            return "child result"

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "child_agent", "capabilities": {"test": 10.0}},
        )

        assert handle == "parent-001::child_agent-0"
        assert isinstance(handle, str)
        # Spawn logged immediately (before join)
        assert len(checkpoint.syscall_log) == 1
        assert checkpoint.syscall_log[0].response == handle

    async def test_join_returns_child_result(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """join_agent awaits child and returns its result."""
        register_search(tool_registry)

        async def child_agent(proxy):
            r = await proxy.syscall("search", {"query": "async-child"})
            return {"answer": r}

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "child_agent", "capabilities": {"test": 10.0}},
        )
        result = await proxy.syscall("join_agent", {"handle": handle})

        assert result == {"answer": ["result for async-child"]}
        # 2 records: spawn_agent_async + join_agent
        assert len(checkpoint.syscall_log) == 2
        join_record = checkpoint.syscall_log[1]
        assert join_record.child_checkpoint is not None
        assert join_record.child_checkpoint.status == "COMPLETED"

    async def test_fan_out_fan_in(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Spawn 3 children async, join all 3, verify all results."""
        register_search(tool_registry)

        async def researcher(proxy):
            return f"result-{proxy.checkpoint.pid}"

        agent_registry.register("researcher", researcher)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handles = []
        for i in range(3):
            h = await proxy.syscall(
                "spawn_agent_async",
                {"agent_name": "researcher", "capabilities": {"test": 5.0}},
            )
            handles.append(h)

        results = []
        for h in handles:
            r = await proxy.syscall("join_agent", {"handle": h})
            results.append(r)

        assert len(results) == 3
        assert results[0] == "result-parent-001::researcher-0"
        assert results[1] == "result-parent-001::researcher-1"
        assert results[2] == "result-parent-001::researcher-2"

    async def test_async_budget_delegation(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Budget delegated at spawn, reclaimed at join."""
        register_search(tool_registry)

        async def child_agent(proxy):
            await proxy.syscall("search", {"query": "x"})
            return "done"

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        # After spawn: 20.0 delegated from parent's 100.0
        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "child_agent", "capabilities": {"test": 20.0}},
        )
        assert checkpoint.capabilities["test"].current_usage == 20.0

        # After join: child used 1.0 of 20.0, 19.0 reclaimed
        await proxy.syscall("join_agent", {"handle": handle})
        assert checkpoint.capabilities["test"].current_usage == 1.0

    async def test_async_deterministic_pids(self, gate, budget_mgr, agent_registry):
        """PIDs follow parent::agent-N counting spawn_agent_async records."""

        async def noop(proxy):
            return "ok"

        agent_registry.register("worker", noop)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        h0 = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )
        h1 = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )

        assert h0 == "parent-001::worker-0"
        assert h1 == "parent-001::worker-1"

        # Join both to avoid dangling tasks
        await proxy.syscall("join_agent", {"handle": h0})
        await proxy.syscall("join_agent", {"handle": h1})

    async def test_parent_continues_between_spawn_and_join(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Parent can do other syscalls between spawn_async and join."""
        register_search(tool_registry)

        async def child_agent(proxy):
            return "child done"

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "child_agent", "capabilities": {"test": 5.0}},
        )

        # Parent does its own work while child runs
        search_result = await proxy.syscall("search", {"query": "parent-work"})
        assert search_result == ["result for parent-work"]

        result = await proxy.syscall("join_agent", {"handle": handle})
        assert result == "child done"

        # 3 records: spawn_async, search, join
        assert len(checkpoint.syscall_log) == 3
        assert checkpoint.syscall_log[0].request["tool_name"] == "spawn_agent_async"
        assert checkpoint.syscall_log[1].request["tool_name"] == "search"
        assert checkpoint.syscall_log[2].request["tool_name"] == "join_agent"


class TestSpawnAsyncMixed:
    async def test_mixed_sync_async_no_pid_collision(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Sync and async spawns of the same agent get unique PIDs."""
        register_search(tool_registry)

        async def worker(proxy):
            return f"done-{proxy.checkpoint.pid}"

        agent_registry.register("worker", worker)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        # Sync spawn first
        sync_result = await proxy.syscall(
            "spawn_agent",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )
        assert sync_result == "done-parent-001::worker-0"

        # Async spawn second — should get worker-1, not worker-0
        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )
        assert handle == "parent-001::worker-1"

        async_result = await proxy.syscall("join_agent", {"handle": handle})
        assert async_result == "done-parent-001::worker-1"

    async def test_async_then_sync_no_pid_collision(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """Async spawn first, then sync spawn — PIDs stay unique."""
        register_search(tool_registry)

        async def worker(proxy):
            return f"done-{proxy.checkpoint.pid}"

        agent_registry.register("worker", worker)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        # Async spawn first
        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )
        assert handle == "parent-001::worker-0"
        await proxy.syscall("join_agent", {"handle": handle})

        # Sync spawn second — should get worker-1
        sync_result = await proxy.syscall(
            "spawn_agent",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )
        assert sync_result == "done-parent-001::worker-1"


class TestSpawnAsyncErrors:
    async def test_join_unknown_handle_raises(self, gate, budget_mgr, agent_registry):
        """join_agent with invalid handle raises RuntimeError."""
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        with pytest.raises(RuntimeError, match="Unknown async agent handle"):
            await proxy.syscall("join_agent", {"handle": "ghost-handle"})

    async def test_async_child_exception_reclaims_budget(
        self, gate, budget_mgr, agent_registry
    ):
        """If async child crashes, budget is reclaimed at join."""

        async def crashing_child(proxy):
            raise RuntimeError("async child exploded")

        agent_registry.register("crashing_child", crashing_child)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "crashing_child", "capabilities": {"test": 20.0}},
        )

        with pytest.raises(RuntimeError, match="async child exploded"):
            await proxy.syscall("join_agent", {"handle": handle})

        # Budget fully reclaimed
        assert checkpoint.capabilities["test"].current_usage == 0.0

    async def test_async_no_registry_raises(self, gate, budget_mgr):
        """spawn_agent_async without AgentRegistry raises RuntimeError."""
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry=None)

        with pytest.raises(RuntimeError, match="AgentRegistry"):
            await proxy.syscall(
                "spawn_agent_async",
                {"agent_name": "x", "capabilities": {}},
            )


class TestSpawnAsyncHITL:
    async def test_async_child_hitl_suspends_at_join(
        self, tool_registry, gate, budget_mgr, agent_registry
    ):
        """When async child suspends for HITL, parent suspends at join_agent."""
        register_delete(tool_registry)

        async def dangerous_child(proxy):
            await proxy.syscall("delete_files", {"paths": ["/important"]})
            return "deleted"

        agent_registry.register("dangerous_child", dangerous_child)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {
                "agent_name": "dangerous_child",
                "capabilities": {"test": 10.0},
            },
        )

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall("join_agent", {"handle": handle})

        assert checkpoint.status == "SUSPENDED_FOR_HITL"
        assert checkpoint.pending_hitl["tool_name"] == "join_agent"
        assert checkpoint.pending_hitl["child_pid"].startswith("parent-001::")

        # Child checkpoint saved in join record
        join_record = checkpoint.syscall_log[-1]
        assert join_record.child_checkpoint is not None
        assert join_record.child_checkpoint.status == "SUSPENDED_FOR_HITL"

    async def test_async_approve_child_hitl(
        self, tool_registry, gate, budget_mgr, agent_registry, handler
    ):
        """Approve async child HITL, resume, parent gets result."""
        register_delete(tool_registry)

        async def dangerous_child(proxy):
            result = await proxy.syscall("delete_files", {"paths": ["/a"]})
            return {"deleted": result}

        agent_registry.register("dangerous_child", dangerous_child)
        checkpoint = make_checkpoint(budget_mgr)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {
                "agent_name": "dangerous_child",
                "capabilities": {"test": 10.0},
            },
        )

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall("join_agent", {"handle": handle})

        # is_child_hitl should recognize join_agent as child HITL
        assert handler.is_child_hitl(checkpoint) is True

        await handler.approve_child_hitl(checkpoint, gate, budget_mgr, agent_registry)

        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None
        last = checkpoint.syscall_log[-1]
        assert last.child_checkpoint.status == "COMPLETED"
        assert last.response == {"deleted": 1}


class TestAsyncSpawnPersistence:
    async def test_child_persisted_at_spawn(
        self, tool_registry, gate, budget_mgr, agent_registry, tmp_path
    ):
        """Child checkpoint is persisted to store immediately at async spawn."""
        from castor.scheduler.persistence import CheckpointStore

        store = CheckpointStore(f"sqlite:///{tmp_path / 'test.db'}")

        async def child_agent(proxy):
            return "child done"

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(budget_mgr)
        store.save(checkpoint)
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)
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


class TestSpawnAsyncReplay:
    async def test_spawn_async_replay_returns_cached_handle(
        self, gate, budget_mgr, agent_registry
    ):
        """On replay, spawn_agent_async returns cached handle without launching task."""
        checkpoint = make_checkpoint(
            budget_mgr,
            syscall_log=[
                SyscallRecord(
                    request={
                        "tool_name": "spawn_agent_async",
                        "arguments": {
                            "agent_name": "worker",
                            "capabilities": {"test": 5.0},
                        },
                    },
                    response="parent-001::worker-0",
                )
            ],
        )
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )

        assert handle == "parent-001::worker-0"
        # No task was launched
        assert len(proxy._async_tasks) == 0

    async def test_join_replay_returns_cached_result(
        self, gate, budget_mgr, agent_registry
    ):
        """On replay, join_agent returns cached result without awaiting task."""
        child_cp = AgentCheckpoint(
            pid="parent-001::worker-0",
            parent_pid="parent-001",
            status="COMPLETED",
            agent_function_name="worker",
            capabilities=budget_mgr.create_budgets({"test": 5.0}),
            result="cached async result",
        )
        checkpoint = make_checkpoint(
            budget_mgr,
            syscall_log=[
                SyscallRecord(
                    request={
                        "tool_name": "spawn_agent_async",
                        "arguments": {
                            "agent_name": "worker",
                            "capabilities": {"test": 5.0},
                        },
                    },
                    response="parent-001::worker-0",
                ),
                SyscallRecord(
                    request={
                        "tool_name": "join_agent",
                        "arguments": {"handle": "parent-001::worker-0"},
                    },
                    response="cached async result",
                    child_checkpoint=child_cp,
                ),
            ],
        )
        proxy = make_proxy(checkpoint, gate, budget_mgr, agent_registry)

        handle = await proxy.syscall(
            "spawn_agent_async",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )
        result = await proxy.syscall("join_agent", {"handle": handle})

        assert result == "cached async result"
        assert not proxy.is_replaying
