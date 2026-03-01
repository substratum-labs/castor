"""Tests for sub-agent spawning: AgentRegistry, SyscallProxy spawn, child HITL."""

import pytest

from castor.capability.manager import CapabilityManager, InsufficientBudgetError
from castor.dam.decorator import castor_tool
from castor.dam.registry import ToolRegistry
from castor.dam.validator import CastorDam
from castor.models.checkpoint import (
    AgentCheckpoint,
    SuspendInterrupt,
    SyscallRecord,
)
from castor.stream.agent_registry import AgentNotFoundError, AgentRegistry, castor_agent
from castor.stream.hitl import HITLHandler
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner

# ── Fixtures ──


@pytest.fixture
def tool_registry():
    return ToolRegistry()


@pytest.fixture
def dam(tool_registry):
    return CastorDam(tool_registry)


@pytest.fixture
def cap_mgr():
    return CapabilityManager()


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


def make_checkpoint(cap_mgr, caps=None, syscall_log=None):
    if caps is None:
        caps = cap_mgr.create_capabilities({"test": 100.0})
    return AgentCheckpoint(
        pid="parent-001",
        status="RUNNING",
        agent_function_name="parent_agent",
        capabilities=caps,
        syscall_log=syscall_log or [],
    )


def make_proxy(checkpoint, dam, cap_mgr, agent_registry=None, lodge=None):
    return SyscallProxy(
        checkpoint=checkpoint,
        dam=dam,
        capability_manager=cap_mgr,
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

    def test_decorator_requires_registry(self):
        with pytest.raises(TypeError):

            @castor_agent(name="oops")
            async def bad_agent(proxy):
                return "never"


# ── Spawn via SyscallProxy ──


class TestSpawnHappyPath:
    async def test_spawn_child_completes(
        self, tool_registry, dam, cap_mgr, agent_registry
    ):
        """Child agent runs, returns result, budget reclaimed."""
        register_search(tool_registry)

        async def child_agent(proxy):
            r = await proxy.syscall("search", {"query": "child"})
            return {"answer": r}

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

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
        self, tool_registry, dam, cap_mgr, agent_registry
    ):
        """Parent budget is deducted by delegation, child uses some, rest reclaimed."""
        register_search(tool_registry)

        async def child_agent(proxy):
            await proxy.syscall("search", {"query": "x"})
            return "done"

        agent_registry.register("child_agent", child_agent)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        # Parent starts with 100 test budget
        await proxy.syscall(
            "spawn_agent",
            {"agent_name": "child_agent", "capabilities": {"test": 20.0}},
        )

        # Child used 1.0 of 20.0, so 19.0 reclaimed to parent
        # Parent usage: 20 delegated - 19 reclaimed = 1.0 net
        assert checkpoint.capabilities["test"].current_usage == 1.0

    async def test_spawn_child_pid_deterministic(
        self, tool_registry, dam, cap_mgr, agent_registry
    ):
        """Child PID follows parent_pid::agent_name-N pattern."""

        async def noop_agent(proxy):
            return "ok"

        agent_registry.register("worker", noop_agent)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        await proxy.syscall(
            "spawn_agent",
            {"agent_name": "worker", "capabilities": {"test": 5.0}},
        )

        child_cp = checkpoint.syscall_log[0].child_checkpoint
        assert child_cp.pid == "parent-001::worker-0"
        assert child_cp.parent_pid == "parent-001"

    async def test_spawn_multiple_children(
        self, tool_registry, dam, cap_mgr, agent_registry
    ):
        """Spawning two children yields incrementing PIDs."""

        async def noop_agent(proxy):
            return "ok"

        agent_registry.register("worker", noop_agent)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

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
    async def test_spawn_no_registry_raises(self, dam, cap_mgr):
        """spawn_agent without AgentRegistry raises RuntimeError."""
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry=None)

        with pytest.raises(RuntimeError, match="AgentRegistry"):
            await proxy.syscall(
                "spawn_agent",
                {"agent_name": "x", "capabilities": {}},
            )

    async def test_spawn_unknown_agent_raises(self, dam, cap_mgr, agent_registry):
        """spawn_agent with unregistered name raises AgentNotFoundError."""
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        with pytest.raises(AgentNotFoundError, match="ghost"):
            await proxy.syscall(
                "spawn_agent",
                {"agent_name": "ghost", "capabilities": {"test": 1.0}},
            )

    async def test_spawn_insufficient_budget_raises(self, dam, cap_mgr, agent_registry):
        """Requesting more budget than parent has raises InsufficientBudgetError."""

        async def noop(proxy):
            return "ok"

        agent_registry.register("noop", noop)
        caps = cap_mgr.create_capabilities({"test": 5.0})
        checkpoint = make_checkpoint(cap_mgr, caps=caps)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        with pytest.raises(InsufficientBudgetError):
            await proxy.syscall(
                "spawn_agent",
                {"agent_name": "noop", "capabilities": {"test": 999.0}},
            )

    async def test_spawn_child_exception_reclaims_budget(
        self, dam, cap_mgr, agent_registry
    ):
        """If child raises unexpected exception, delegated budget is reclaimed."""

        async def crashing_child(proxy):
            raise RuntimeError("child exploded")

        agent_registry.register("crashing_child", crashing_child)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

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
        self, tool_registry, dam, cap_mgr, agent_registry
    ):
        """When child hits HITL, parent also suspends."""
        register_delete(tool_registry)

        async def dangerous_child(proxy):
            await proxy.syscall("delete_files", {"paths": ["/important"]})
            return "deleted"

        agent_registry.register("dangerous_child", dangerous_child)
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

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
    async def test_is_child_hitl(self, handler, cap_mgr):
        checkpoint = make_checkpoint(cap_mgr)
        assert handler.is_child_hitl(checkpoint) is False

        checkpoint.pending_hitl = {
            "tool_name": "spawn_agent",
            "arguments": {"agent_name": "x"},
        }
        assert handler.is_child_hitl(checkpoint) is True

    async def test_approve_child_hitl(
        self, tool_registry, dam, cap_mgr, agent_registry, handler
    ):
        """Approving child HITL executes the child's blocked tool and resumes."""
        register_delete(tool_registry)

        async def dangerous_child(proxy):
            result = await proxy.syscall("delete_files", {"paths": ["/a"]})
            return {"deleted": result}

        agent_registry.register("dangerous_child", dangerous_child)

        # First: spawn and get suspended
        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "dangerous_child",
                    "capabilities": {"test": 10.0},
                },
            )

        # Now approve the child HITL
        await handler.approve_child_hitl(checkpoint, dam, cap_mgr, agent_registry)

        assert checkpoint.status == "RUNNING"
        assert checkpoint.pending_hitl is None

        # Last record should have child's result
        last = checkpoint.syscall_log[-1]
        assert last.child_checkpoint.status == "COMPLETED"
        assert last.response == {"deleted": 1}

    async def test_reject_child_hitl(
        self, tool_registry, dam, cap_mgr, agent_registry, handler
    ):
        """Rejecting child HITL logs feedback and resumes child (which completes)."""
        register_delete(tool_registry)

        async def child_with_fallback(proxy):
            result = await proxy.syscall("delete_files", {"paths": ["/a"]})
            # After HITL_REJECTED, the LLM would see the rejection and return
            return {"result": result}

        agent_registry.register("child_with_fallback", child_with_fallback)

        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "child_with_fallback",
                    "capabilities": {"test": 10.0},
                },
            )

        await handler.reject_child_hitl(
            checkpoint, "Too dangerous", dam, cap_mgr, agent_registry
        )

        assert checkpoint.status == "RUNNING"
        # Child should have the rejection in its log
        child_cp = checkpoint.syscall_log[-1].child_checkpoint
        rejection_record = child_cp.syscall_log[0]
        assert rejection_record.response["status"] == "HITL_REJECTED"

    async def test_modify_child_hitl(
        self, tool_registry, dam, cap_mgr, agent_registry, handler
    ):
        """Modifying child HITL logs modification feedback and resumes."""
        register_delete(tool_registry)

        async def child_agent(proxy):
            result = await proxy.syscall("delete_files", {"paths": ["/a"]})
            return {"result": result}

        agent_registry.register("child_agent", child_agent)

        checkpoint = make_checkpoint(cap_mgr)
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        with pytest.raises(SuspendInterrupt):
            await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "child_agent",
                    "capabilities": {"test": 10.0},
                },
            )

        await handler.modify_child_hitl(
            checkpoint, "Only delete temp files", dam, cap_mgr, agent_registry
        )

        assert checkpoint.status == "RUNNING"
        child_cp = checkpoint.syscall_log[-1].child_checkpoint
        mod_record = child_cp.syscall_log[0]
        assert mod_record.response["status"] == "HITL_MODIFIED"
        assert "temp files" in mod_record.response["human_feedback"]

    async def test_approve_child_no_spawn_raises(
        self, dam, cap_mgr, agent_registry, handler
    ):
        """approve_child_hitl raises if pending_hitl is not a spawn."""
        checkpoint = make_checkpoint(cap_mgr)
        checkpoint.pending_hitl = {
            "tool_name": "delete_files",
            "arguments": {"paths": ["/a"]},
        }
        with pytest.raises(ValueError, match="use approve"):
            await handler.approve_child_hitl(checkpoint, dam, cap_mgr, agent_registry)


# ── AgentRunner with AgentRegistry ──


class TestAgentRunnerWithRegistry:
    async def test_runner_passes_registry_to_proxy(
        self, tool_registry, dam, cap_mgr, agent_registry
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

        runner = AgentRunner(dam, cap_mgr, agent_registry=agent_registry)
        checkpoint = make_checkpoint(cap_mgr)
        checkpoint.agent_function_name = "parent_agent"

        result = await runner.run(parent_agent, checkpoint)

        assert result.status == "COMPLETED"
        assert result.result == "parent done"
        assert spawned == [["result for from-child"]]


# ── Spawn replay ──


class TestSpawnReplay:
    async def test_spawn_result_replayed_from_cache(
        self, tool_registry, dam, cap_mgr, agent_registry
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
            capabilities=cap_mgr.create_capabilities({"test": 10.0}),
            result="cached child result",
        )
        checkpoint = make_checkpoint(
            cap_mgr,
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
        proxy = make_proxy(checkpoint, dam, cap_mgr, agent_registry)

        result = await proxy.syscall(
            "spawn_agent",
            {"agent_name": "child_agent", "capabilities": {"test": 10.0}},
        )

        assert result == "cached child result"
        assert proxy._replay_index == 1
        assert not proxy.is_replaying
