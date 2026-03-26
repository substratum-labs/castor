"""Tests for V2 API improvements (P1-P8)."""

import asyncio

import pytest

from castor import (
    AgentCheckpoint,
    AgentRegistry,
    Castor,
    CastorTask,
    LLMSyscall,
    StreamingLLMSyscall,
    SyscallProxy,
    SyscallResult,
    auto_approve,
    auto_reject,
    castor_agent,
    castor_tool,
    default_agent_registry,
)
from castor.capability.manager import CapabilityManager
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.scheduler.persistence import MemoryCheckpointStore
from castor.scheduler.proxy import SyscallProxy as ProxyClass

# ── Fixtures ──


@pytest.fixture
def _clean_default_registries():
    """Clean up default registries after each test."""
    yield
    default_agent_registry._agents.clear()


# ── P3: @castor_tool defaults ──


class TestCastorToolDefaults:
    def test_zero_arg_decorator(self):
        """@castor_tool() works with no arguments."""
        registry = ToolRegistry()

        @castor_tool(registry=registry)
        async def greet(name: str) -> str:
            return f"hi {name}"

        meta = registry.get("greet")
        assert meta.consumes == "_default"
        assert meta.cost_per_use == 0.0

    def test_hitl_only_no_budget(self):
        """@castor_tool(requires_hitl=True) works without consumes."""
        registry = ToolRegistry()

        @castor_tool(requires_hitl=True, registry=registry)
        def delete_files(paths: list[str]) -> int:
            return len(paths)

        meta = registry.get("delete_files")
        assert meta.requires_hitl is True
        assert meta.cost_per_use == 0.0

    @pytest.mark.asyncio
    async def test_zero_cost_tool_skips_deduction(self):
        """Zero-cost tools run even without matching capability budget."""

        @castor_tool(registry=ToolRegistry())
        async def ping() -> str:
            return "pong"

        registry = ToolRegistry()
        registry.register(ping._castor_metadata)
        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()

        # No budgets at all — should still work for zero-cost tool
        cp = AgentCheckpoint(
            pid="test", status="RUNNING", agent_function_name="agent", capabilities={}
        )
        proxy = ProxyClass(cp, gate, cap_mgr)
        result = await proxy.syscall("ping")
        assert result == "pong"

    @pytest.mark.asyncio
    async def test_budget_tool_still_works(self):
        """Tools with explicit consumes/cost_per_use still enforce budgets."""
        registry = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
        async def search(query: str) -> str:
            return f"results for {query}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        caps = cap_mgr.create_capabilities({"api": 2.0})
        cp = AgentCheckpoint(
            pid="test", status="RUNNING", agent_function_name="agent", capabilities=caps
        )
        proxy = ProxyClass(cp, gate, cap_mgr)

        result = await proxy.syscall("search", query="hello")
        assert result == "results for hello"
        assert caps["api"].current_usage == 1.0

    @pytest.mark.asyncio
    async def test_zero_cost_hitl_tool_suspends(self):
        """Zero-cost HITL tools suspend correctly without budget errors."""
        registry = ToolRegistry()

        @castor_tool(requires_hitl=True, registry=registry)
        def danger() -> str:
            return "done"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.danger()

        cp = await kernel.run(agent, pid="test-hitl")
        assert cp.is_suspended


# ── P7: Checkpoint convenience properties ──


class TestCheckpointConvenience:
    def test_pending_tool_and_args(self):
        cp = AgentCheckpoint(
            pid="test",
            status="SUSPENDED_FOR_HITL",
            agent_function_name="agent",
            capabilities={},
            pending_hitl={
                "tool_name": "delete_files",
                "arguments": {"paths": ["/tmp/a"]},
            },
        )
        assert cp.pending_tool == "delete_files"
        assert cp.pending_args == {"paths": ["/tmp/a"]}

    def test_pending_tool_none_when_not_suspended(self):
        cp = AgentCheckpoint(
            pid="test",
            status="RUNNING",
            agent_function_name="agent",
            capabilities={},
        )
        assert cp.pending_tool is None
        assert cp.pending_args is None

    def test_budget_helpers(self):
        from castor.models.capability import Capability

        cp = AgentCheckpoint(
            pid="test",
            status="RUNNING",
            agent_function_name="agent",
            capabilities={
                "api": Capability(
                    resource_type="api", max_budget=10.0, current_usage=3.0
                )
            },
        )
        assert cp.budget_used("api") == 3.0
        assert cp.budget_remaining("api") == 7.0
        assert cp.budget_used("missing") == 0.0
        assert cp.budget_remaining("missing") == 0.0

    def test_is_suspended_and_is_complete(self):
        cp = AgentCheckpoint(
            pid="test",
            status="SUSPENDED_FOR_HITL",
            agent_function_name="agent",
            capabilities={},
        )
        assert cp.is_suspended is True
        assert cp.is_complete is False

        cp.status = "COMPLETED"
        assert cp.is_suspended is False
        assert cp.is_complete is True


# ── P8: proxy.budget() ──


class TestProxyBudget:
    @pytest.mark.asyncio
    async def test_budget_returns_remaining(self):
        from castor.models.capability import Capability

        registry = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
        async def search(query: str) -> str:
            return "ok"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        caps = {
            "api": Capability(resource_type="api", max_budget=10.0, current_usage=3.0)
        }
        cp = AgentCheckpoint(
            pid="test", status="RUNNING", agent_function_name="agent", capabilities=caps
        )
        proxy = ProxyClass(cp, gate, cap_mgr)

        assert proxy.budget("api") == 7.0
        assert proxy.budget("missing") == 0.0


# ── P1: SyscallResult ──


class TestSyscallResult:
    def test_ok_result(self):
        r = SyscallResult(value="hello")
        assert r.ok is True
        assert r.value == "hello"
        assert r.rejected is False
        assert r.modified is False
        assert r.exhausted is False

    def test_rejected_result(self):
        r = SyscallResult(status="HITL_REJECTED", feedback="too risky")
        assert r.rejected is True
        assert r.ok is False
        assert r.feedback == "too risky"
        assert r.value is None

    def test_modified_result(self):
        r = SyscallResult(status="HITL_MODIFIED", feedback="use staging")
        assert r.modified is True
        assert r.feedback == "use staging"

    def test_exhausted_result(self):
        r = SyscallResult(
            status="INSUFFICIENT_CAPABILITY", feedback="Budget exceeded", resource="api"
        )
        assert r.exhausted is True
        assert r.resource == "api"

    def test_repr(self):
        r = SyscallResult(value=42)
        assert "42" in repr(r)

    @pytest.mark.asyncio
    async def test_structured_results_non_hitl_returns_raw(self):
        """With structured_results=True, non-destructive tools return raw values."""
        registry = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
        async def search(query: str) -> str:
            return f"results for {query}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, structured_results=True)

        async def agent(proxy: SyscallProxy) -> str:
            r1 = await proxy.search(query="hello")
            assert isinstance(r1, str)  # NOT SyscallResult
            return r1

        cp = await kernel.run(agent, budgets={"api": 10.0}, pid="test-sr")
        assert cp.is_complete
        assert cp.result == "results for hello"

    @pytest.mark.asyncio
    async def test_structured_results_ok_wrapping(self):
        """After HITL approval, destructive tools return SyscallResult(ok)."""
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def send_email(to: str) -> str:
            return f"sent to {to}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, structured_results=True)

        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.send_email(to="team@co.com")
            assert isinstance(result, SyscallResult)
            assert result.ok
            assert result.value == "sent to team@co.com"
            return "done"

        # Run, approve, resume
        cp = await kernel.run(agent, budgets={"api": 10.0}, pid="test-ok")
        await kernel.approve(cp)
        cp = await kernel.run(agent, checkpoint=cp)
        assert cp.is_complete

    @pytest.mark.asyncio
    async def test_structured_results_rejected(self):
        """After HITL rejection, destructive tools return SyscallResult(rejected)."""
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def send_email(to: str) -> str:
            return f"sent to {to}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, structured_results=True)

        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.send_email(to="team@co.com")
            assert isinstance(result, SyscallResult)
            assert result.rejected
            assert result.feedback == "not safe"
            return "handled rejection"

        cp = await kernel.run(agent, budgets={"api": 10.0}, pid="test-rej")
        kernel.reject(cp, "not safe")
        cp = await kernel.run(agent, checkpoint=cp)
        assert cp.is_complete
        assert cp.result == "handled rejection"

    @pytest.mark.asyncio
    async def test_structured_results_off_returns_raw(self):
        """Without structured_results, everything returns raw values."""
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def send_email(to: str) -> str:
            return f"sent to {to}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)  # no structured_results

        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.send_email(to="team@co.com")
            assert isinstance(result, dict)  # old behavior
            return "done"

        cp = await kernel.run(agent, budgets={"api": 10.0}, pid="test-raw")
        kernel.reject(cp, "nope")
        cp = await kernel.run(agent, checkpoint=cp)
        assert cp.is_complete


# ── P2: run_until_complete ──


class TestRunUntilComplete:
    @pytest.mark.asyncio
    async def test_auto_approve(self):
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def send_email(to: str) -> str:
            return f"sent to {to}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.send_email(to="team@co.com")

        cp = await kernel.run_until_complete(
            agent, budgets={"api": 10.0}, on_hitl=auto_approve, pid="test-auto"
        )
        assert cp.is_complete
        assert cp.result == "sent to team@co.com"

    @pytest.mark.asyncio
    async def test_auto_reject(self):
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def send_email(to: str) -> str:
            return f"sent to {to}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)

        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.send_email(to="team@co.com")
            if isinstance(result, dict) and result.get("status") == "HITL_REJECTED":
                return "rejected"
            return "sent"

        cp = await kernel.run_until_complete(
            agent, budgets={"api": 10.0}, on_hitl=auto_reject, pid="test-reject"
        )
        assert cp.is_complete
        assert cp.result == "rejected"

    @pytest.mark.asyncio
    async def test_custom_policy(self):
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def send_email(to: str) -> str:
            return f"sent to {to}"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)

        async def my_policy(cp):
            return ("modify", "add CC: boss@co.com")

        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.send_email(to="team@co.com")
            if isinstance(result, dict) and result.get("status") == "HITL_MODIFIED":
                return f"modified: {result['human_feedback']}"
            return "sent"

        cp = await kernel.run_until_complete(
            agent, budgets={"api": 10.0}, on_hitl=my_policy, pid="test-custom"
        )
        assert cp.is_complete
        assert "CC: boss@co.com" in cp.result

    @pytest.mark.asyncio
    async def test_max_iterations_guard(self):
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=0.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def danger() -> str:
            return "done"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)

        async def agent(proxy: SyscallProxy) -> str:
            # Each call to danger() triggers a separate HITL suspend.
            # With max_iterations=3, the 4th suspend exceeds the limit.
            await proxy.danger()
            await proxy.danger()
            await proxy.danger()
            await proxy.danger()
            return "never"

        with pytest.raises(RuntimeError, match="exceeded 3 HITL iterations"):
            await kernel.run_until_complete(
                agent, on_hitl=auto_approve, pid="test-max", max_iterations=3
            )


# ── P6: spawn/join sugar + default agent registry ──


class TestSpawnJoinSugar:
    @pytest.mark.asyncio
    async def test_spawn_and_join(self):
        registry = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
        async def search(query: str) -> str:
            return f"found: {query}"

        agent_reg = AgentRegistry()

        @castor_agent(name="worker", registry=agent_reg)
        async def worker(proxy: SyscallProxy) -> str:
            return await proxy.search(query="test")

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, agent_registry=agent_reg)

        async def coordinator(proxy: SyscallProxy) -> str:
            handle = await proxy.spawn("worker", capabilities={"api": 5.0})
            result = await proxy.join(handle)
            return f"child said: {result}"

        cp = await kernel.run(coordinator, budgets={"api": 10.0}, pid="coord")
        assert cp.is_complete
        assert "found: test" in cp.result

    @pytest.mark.asyncio
    async def test_spawn_sync(self):
        registry = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
        async def ping() -> str:
            return "pong"

        agent_reg = AgentRegistry()

        @castor_agent(name="echo", registry=agent_reg)
        async def echo(proxy: SyscallProxy) -> str:
            return await proxy.ping()

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, agent_registry=agent_reg)

        async def parent(proxy: SyscallProxy) -> str:
            return await proxy.spawn_sync("echo", capabilities={"api": 5.0})

        cp = await kernel.run(parent, budgets={"api": 10.0}, pid="sync-parent")
        assert cp.is_complete
        assert cp.result == "pong"

    def test_default_agent_registry(self, _clean_default_registries):
        """@castor_agent without registry uses default_agent_registry."""

        @castor_agent(name="auto_worker")
        async def auto_worker(proxy: SyscallProxy) -> str:
            return "auto"

        assert default_agent_registry.has_agent("auto_worker")


# ── P4: LLMSyscall facade integration ──


class TestLLMFacade:
    def test_llm_syscall_lazy_mode(self):
        """LLMSyscall without registry stores metadata for facade."""

        async def fake_llm(prompt: str) -> str:
            return f"answer: {prompt}"

        llm = LLMSyscall(call_fn=fake_llm)
        assert llm._metadata is not None
        assert llm._metadata.tool_name == "llm_inference"

    def test_streaming_llm_lazy_mode(self):
        """StreamingLLMSyscall without registry stores metadata."""

        async def fake_stream(prompt: str):
            yield "hello"

        llm = StreamingLLMSyscall(stream_fn=fake_stream)
        assert llm._metadata is not None
        assert llm._metadata.tool_name == "llm_inference_streaming"

    def test_llm_in_tools_list(self):
        """Castor(tools=[llm_instance]) registers the LLM tool."""

        async def fake_llm(prompt: str) -> str:
            return "ok"

        llm = LLMSyscall(call_fn=fake_llm)

        registry_for_tool = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry_for_tool)
        async def search(query: str) -> str:
            return "ok"

        kernel = Castor(tools=[search, llm])
        assert kernel._gate.has_tool("llm_inference")
        assert kernel._gate.has_tool("search")

    def test_llm_with_explicit_registry_still_works(self):
        """Passing registry= still registers immediately (backward compat)."""
        reg = ToolRegistry()

        async def fake_llm(prompt: str) -> str:
            return "ok"

        LLMSyscall(reg, call_fn=fake_llm)
        assert reg.has_tool("llm_inference")

    @pytest.mark.asyncio
    async def test_llm_end_to_end_via_facade(self):
        """Full LLM inference through the facade."""

        async def fake_llm(prompt: str) -> str:
            return f"answer: {prompt}"

        llm = LLMSyscall(call_fn=fake_llm, consumes="api_usd", cost_per_use=0.5)
        kernel = Castor(tools=[llm])

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, prompt="hello")

        cp = await kernel.run(agent, budgets={"api_usd": 5.0}, pid="llm-e2e")
        assert cp.is_complete
        assert cp.result == "answer: hello"


# ── P5: Preemption facade ──


class TestPreemptionFacade:
    @pytest.mark.asyncio
    async def test_run_async_and_await(self):
        """run_async returns CastorTask that can be awaited."""
        registry = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
        async def search(query: str) -> str:
            return "ok"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.search(query="test")

        task = await kernel.run_async(agent, budgets={"api": 10.0})
        assert isinstance(task, CastorTask)
        cp = await task
        assert cp.is_complete
        assert cp.result == "ok"

    @pytest.mark.asyncio
    async def test_preempt_running_task(self):
        """kernel.preempt() cancels a running agent."""
        registry = ToolRegistry()

        @castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
        async def slow_search(query: str) -> str:
            await asyncio.sleep(10)
            return "ok"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        kernel = Castor(gate=gate, capability_manager=cap_mgr)

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.slow_search(query="test")

        task = await kernel.run_async(agent, budgets={"api": 10.0})
        await asyncio.sleep(0.05)  # let agent start
        kernel.preempt(task, reason="timeout", payload={"elapsed": 5})
        cp = await task
        assert cp.status == "PREEMPTED"
        assert cp.preemption_reason == "timeout"
        assert cp.preemption_payload == {"elapsed": 5}


# ── Exports ──


class TestPIDBasedHITL:
    """HITL approve/reject/modify accept a PID string (loads from store)."""

    @pytest.mark.asyncio
    async def test_approve_by_pid(self):
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def danger() -> str:
            return "done"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        store = MemoryCheckpointStore()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, store=store)

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.danger()

        cp = await kernel.run(agent, budgets={"api": 10.0}, pid="pid-hitl")
        assert cp.is_suspended
        store.save(cp)

        # Approve by PID string — auto-saves back to store
        await kernel.approve("pid-hitl")
        cp = store.load("pid-hitl")
        cp = await kernel.run(agent, checkpoint=cp)
        assert cp.is_complete
        assert cp.result == "done"

    @pytest.mark.asyncio
    async def test_reject_by_pid(self):
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def danger() -> str:
            return "done"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        store = MemoryCheckpointStore()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, store=store)

        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.danger()
            if isinstance(result, dict) and result.get("status") == "HITL_REJECTED":
                return "rejected"
            return "sent"

        cp = await kernel.run(agent, budgets={"api": 10.0}, pid="pid-reject")
        assert cp.is_suspended
        store.save(cp)

        kernel.reject("pid-reject", "not allowed")
        cp = store.load("pid-reject")
        cp = await kernel.run(agent, checkpoint=cp)
        assert cp.is_complete
        assert cp.result == "rejected"

    @pytest.mark.asyncio
    async def test_modify_by_pid(self):
        registry = ToolRegistry()

        @castor_tool(
            consumes="api",
            cost_per_use=1.0,
            destructive=True,
            requires_hitl=True,
            registry=registry,
        )
        async def danger() -> str:
            return "done"

        gate = SyscallGate(registry)
        cap_mgr = CapabilityManager()
        store = MemoryCheckpointStore()
        kernel = Castor(gate=gate, capability_manager=cap_mgr, store=store)

        async def agent(proxy: SyscallProxy) -> str:
            result = await proxy.danger()
            if isinstance(result, dict) and result.get("status") == "HITL_MODIFIED":
                return f"modified: {result['human_feedback']}"
            return "sent"

        cp = await kernel.run(agent, budgets={"api": 10.0}, pid="pid-modify")
        assert cp.is_suspended
        store.save(cp)

        kernel.modify("pid-modify", "use safer approach")
        cp = store.load("pid-modify")
        cp = await kernel.run(agent, checkpoint=cp)
        assert cp.is_complete
        assert "safer approach" in cp.result

    def test_pid_without_store_raises(self):
        kernel = Castor()
        with pytest.raises(RuntimeError, match="store"):
            kernel.reject("some-pid", "reason")

    def test_pid_not_found_raises(self):
        store = MemoryCheckpointStore()
        kernel = Castor(store=store)
        with pytest.raises(Exception):
            kernel.reject("nonexistent", "reason")


class TestV2Exports:
    def test_new_exports_available(self):
        """All V2 symbols are importable from castor."""
        from castor import (
            CastorTask,
            SyscallResult,
            auto_approve,
            auto_reject,
            default_agent_registry,
            interactive,
        )

        assert CastorTask is not None
        assert SyscallResult is not None
        assert callable(auto_approve)
        assert callable(auto_reject)
        assert callable(interactive)
        assert default_agent_registry is not None
