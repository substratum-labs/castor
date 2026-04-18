"""Tests for StreamingLLMSyscall — token-level preemption, partial work, budget.

Covers:
- Happy-path streaming completion and accumulation
- Preemption mid-stream saves partial_work on checkpoint
- Replay determinism (cached full response, no re-streaming)
- on_chunk / on_chunk_async callbacks
- Content-based preemption triggered from callback
- Concurrent streaming isolation (parent + child via ContextVar)
- Proportional budget charging on cancellation
- Resume context accessibility (preemption_context property)
- Preemption context cleared on successful completion
- Backward compatibility (LLMSyscall unchanged)
"""

from __future__ import annotations

import asyncio

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.llm.wrapper import LLMSyscall, StreamingLLMSyscall
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.agent_registry import AgentRegistry, castor_agent
from castor.scheduler.proxy import SyscallProxy
from castor.scheduler.runner import AgentRunner

# ── Fixtures ──


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def gate(registry):
    return SyscallGate(registry)


@pytest.fixture
def budget_mgr():
    return BudgetManager()


def _make_checkpoint(budget_mgr, budget=10.0):
    caps = budget_mgr.create_budgets({"api_usd": budget})
    return AgentCheckpoint(
        pid="stream-test-001",
        status="RUNNING",
        agent_function_name="streaming_agent",
        capabilities=caps,
    )


# ── Phase 1: Streaming + Partial Work + Callbacks ──


class TestStreamingCompletion:
    """StreamingLLMSyscall accumulates chunks and returns the full text."""

    async def test_streaming_completes_and_accumulates(
        self, registry, gate, budget_mgr
    ):
        async def fake_stream(model: str, prompt: str):
            for word in ["Hello", " ", "World"]:
                yield word

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=fake_stream,
            consumes="api_usd",
            cost_per_use=1.0,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="hi")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        result = await runner.run(agent, checkpoint)

        assert result.status == "COMPLETED"
        assert result.result == "Hello World"
        assert len(result.syscall_log) == 1
        assert result.syscall_log[0].response == "Hello World"
        assert result.syscall_log[0].request["tool_name"] == "llm_inference_streaming"

    async def test_streaming_with_custom_tool_name(self, registry, gate, budget_mgr):
        async def fake_stream(model: str, prompt: str):
            yield "response"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=fake_stream,
            consumes="api_usd",
            cost_per_use=0.5,
            tool_name="claude_streaming",
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="claude", prompt="hi")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        assert checkpoint.syscall_log[0].request["tool_name"] == "claude_streaming"

    async def test_streaming_empty_response(self, registry, gate, budget_mgr):
        async def empty_stream(model: str, prompt: str):
            return
            yield  # noqa: RET504 — make it an async generator

        llm = StreamingLLMSyscall(
            registry, stream_fn=empty_stream, consumes="api_usd", cost_per_use=0.5
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="empty")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        assert checkpoint.result == ""
        assert checkpoint.syscall_log[0].response == ""


class TestStreamingPreemption:
    """CancelledError during streaming saves accumulated text to partial_work."""

    async def test_preemption_saves_partial_work(self, registry, gate, budget_mgr):
        started = asyncio.Event()

        async def slow_stream(model: str, prompt: str):
            yield "The"
            yield " quick"
            started.set()
            await asyncio.sleep(10)  # will be cancelled here
            yield " brown"
            yield " fox"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=slow_stream,
            consumes="api_usd",
            cost_per_use=1.0,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="stream me")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        task = await runner.run_as_task(agent, checkpoint)
        await started.wait()

        runner.preempt("HUMAN_ABORT", {"instruction": "stop now"})

        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint.status == "PREEMPTED"
        assert checkpoint.preemption_reason == "HUMAN_ABORT"
        assert checkpoint.partial_work == "The quick"
        # Syscall was interrupted — not logged
        assert len(checkpoint.syscall_log) == 0

    async def test_preemption_without_streaming_has_no_partial_work(
        self, registry, gate, budget_mgr
    ):
        """Non-streaming preemption still works — partial_work stays None."""
        started = asyncio.Event()

        @castor_tool(consumes="api_usd", cost_per_use=1.0, registry=registry)
        def search(query: str) -> list:
            return [f"result for {query}"]

        async def agent(proxy: SyscallProxy) -> str:
            await proxy.syscall("search", {"query": "start"})
            started.set()
            await asyncio.sleep(10)
            return "done"

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        task = await runner.run_as_task(agent, checkpoint)
        await started.wait()

        runner.preempt("BUDGET_EXHAUSTED")

        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint.status == "PREEMPTED"
        assert checkpoint.partial_work is None


class TestStreamingReplay:
    """Streaming LLM responses are replayed from cache without re-streaming."""

    async def test_streaming_replay_serves_cached_response(
        self, registry, gate, budget_mgr
    ):
        call_count = 0

        async def counting_stream(model: str, prompt: str):
            nonlocal call_count
            call_count += 1
            yield "cached"
            yield " response"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=counting_stream,
            consumes="api_usd",
            cost_per_use=1.0,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="first")

        # First run — live streaming
        checkpoint = _make_checkpoint(budget_mgr)
        runner1 = AgentRunner(gate, budget_mgr)
        await runner1.run(agent, checkpoint)

        assert checkpoint.status == "COMPLETED"
        assert call_count == 1
        assert checkpoint.result == "cached response"

        # Full replay — zero new streaming calls
        runner2 = AgentRunner(gate, budget_mgr)
        replayed = await runner2.run(agent, checkpoint)

        assert replayed.status == "COMPLETED"
        assert call_count == 1  # unchanged — served from cache
        assert replayed.result == "cached response"


class TestStreamingCallbacks:
    """on_chunk / on_chunk_async callbacks fire for every chunk."""

    async def test_on_chunk_callback_fires(self, registry, gate, budget_mgr):
        chunks_seen: list[tuple[str, str]] = []

        def track_chunks(chunk: str, accumulated: str) -> None:
            chunks_seen.append((chunk, accumulated))

        async def fake_stream(model: str, prompt: str):
            yield "Hello"
            yield " "
            yield "World"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=fake_stream,
            consumes="api_usd",
            cost_per_use=1.0,
            on_chunk=track_chunks,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="hi")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        assert len(chunks_seen) == 3
        assert chunks_seen[0] == ("Hello", "Hello")
        assert chunks_seen[1] == (" ", "Hello ")
        assert chunks_seen[2] == ("World", "Hello World")

    async def test_on_chunk_async_callback(self, registry, gate, budget_mgr):
        chunks_seen: list[str] = []

        async def async_tracker(chunk: str, accumulated: str) -> None:
            chunks_seen.append(chunk)

        async def fake_stream(model: str, prompt: str):
            yield "a"
            yield "b"
            yield "c"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=fake_stream,
            consumes="api_usd",
            cost_per_use=1.0,
            on_chunk_async=async_tracker,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="abc")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        assert chunks_seen == ["a", "b", "c"]

    async def test_content_preemption_via_callback(self, registry, gate, budget_mgr):
        """External code can preempt from on_chunk callback."""
        runner = AgentRunner(gate, budget_mgr)
        started = asyncio.Event()

        def danger_detector(chunk: str, accumulated: str) -> None:
            if "delete /prod" in accumulated:
                runner.preempt("POLICY_VIOLATION", {"reason": "dangerous plan"})

        async def slow_stream(model: str, prompt: str):
            yield "I will "
            yield "delete /prod"
            started.set()
            # After the callback fires preempt(), the next await hits CancelledError
            await asyncio.sleep(0)
            yield " successfully"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=slow_stream,
            consumes="api_usd",
            cost_per_use=1.0,
            on_chunk=danger_detector,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="plan")

        checkpoint = _make_checkpoint(budget_mgr)
        task = await runner.run_as_task(agent, checkpoint)
        await started.wait()
        # Give the event loop a tick for cancel to propagate
        await asyncio.sleep(0.01)

        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint.status == "PREEMPTED"
        assert checkpoint.preemption_reason == "POLICY_VIOLATION"
        assert checkpoint.partial_work == "I will delete /prod"


class TestStreamingConcurrency:
    """Parent and child sharing one StreamingLLMSyscall don't interfere."""

    async def test_concurrent_streaming_isolation(self, registry, gate, budget_mgr):
        agent_registry = AgentRegistry()

        async def parent_stream(model: str, prompt: str):
            yield "parent-"
            yield "text"

        async def child_stream(model: str, prompt: str):
            yield "child-"
            yield "text"

        parent_llm = StreamingLLMSyscall(
            registry,
            stream_fn=parent_stream,
            consumes="api_usd",
            cost_per_use=0.5,
            tool_name="parent_llm",
        )

        child_llm = StreamingLLMSyscall(
            registry,
            stream_fn=child_stream,
            consumes="api_usd",
            cost_per_use=0.5,
            tool_name="child_llm",
        )

        @castor_agent(registry=agent_registry)
        async def child_agent(proxy: SyscallProxy) -> str:
            return await child_llm.infer(proxy, model="test", prompt="child")

        async def parent_agent(proxy: SyscallProxy) -> str:
            parent_result = await parent_llm.infer(proxy, model="test", prompt="parent")
            child_result = await proxy.syscall(
                "spawn_agent",
                {"agent_name": "child_agent", "capabilities": {"api_usd": 2.0}},
            )
            return f"{parent_result} | {child_result}"

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr, agent_registry=agent_registry)
        result = await runner.run(parent_agent, checkpoint)

        assert result.status == "COMPLETED"
        assert result.result == "parent-text | child-text"


# ── Phase 2: Resume Context ──


class TestResumeContext:
    """Preemption context is accessible on resume via proxy.preemption_context."""

    async def test_preemption_context_accessible(self, registry, gate, budget_mgr):
        started = asyncio.Event()

        async def fake_stream(model: str, prompt: str):
            yield "partial"
            started.set()
            await asyncio.sleep(10)
            yield " full"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=fake_stream,
            consumes="api_usd",
            cost_per_use=1.0,
        )

        # First run: agent streams, gets preempted
        async def agent_v1(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="stream")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        task = await runner.run_as_task(agent_v1, checkpoint)
        await started.wait()
        runner.preempt("HUMAN_ABORT", {"instruction": "adapt"})

        with pytest.raises(asyncio.CancelledError):
            await task

        assert checkpoint.partial_work == "partial"
        assert checkpoint.preemption_reason == "HUMAN_ABORT"

        # Resume: verify context is available through proxy
        context_seen = None

        async def agent_v2(proxy: SyscallProxy) -> str:
            nonlocal context_seen
            context_seen = proxy.preemption_context
            if context_seen:
                proxy.clear_preemption_context()
            return "adapted"

        checkpoint.status = "RUNNING"
        runner2 = AgentRunner(gate, budget_mgr)
        await runner2.run(agent_v2, checkpoint)

        assert context_seen is not None
        assert context_seen["reason"] == "HUMAN_ABORT"
        assert context_seen["payload"] == {"instruction": "adapt"}
        assert context_seen["partial_work"] == "partial"

    async def test_preemption_context_cleared_on_completion(
        self, registry, gate, budget_mgr
    ):
        """Preemption fields are cleared after successful run."""

        @castor_tool(consumes="api_usd", cost_per_use=0.1, registry=registry)
        def search(query: str) -> str:
            return f"found {query}"

        async def agent(proxy: SyscallProxy) -> str:
            return await proxy.syscall("search", {"query": "test"})

        checkpoint = _make_checkpoint(budget_mgr)
        # Simulate residual preemption context from a prior run
        checkpoint.preemption_reason = "STALE_REASON"
        checkpoint.preemption_payload = {"old": True}
        checkpoint.partial_work = "old partial"

        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        assert checkpoint.status == "COMPLETED"
        # All preemption fields cleared
        assert checkpoint.preemption_reason is None
        assert checkpoint.preemption_payload is None
        assert checkpoint.partial_work is None

    async def test_no_preemption_context_when_not_preempted(
        self, registry, gate, budget_mgr
    ):
        @castor_tool(consumes="api_usd", cost_per_use=0.1, registry=registry)
        def search(query: str) -> str:
            return f"found {query}"

        context_seen = None

        async def agent(proxy: SyscallProxy) -> str:
            nonlocal context_seen
            context_seen = proxy.preemption_context
            return await proxy.syscall("search", {"query": "test"})

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        assert context_seen is None


# ── Phase 3: Proportional Budget ──


class TestProportionalBudget:
    """Proportional budget charges actual tokens consumed on cancellation."""

    async def test_proportional_budget_on_cancel(self, registry, gate, budget_mgr):
        """After streaming cancellation, budget charged for actual tokens only."""
        started = asyncio.Event()

        async def slow_stream(model: str, prompt: str):
            yield "one"  # token 1
            yield " two"  # token 2
            yield " three"  # token 3
            started.set()
            await asyncio.sleep(10)  # cancelled here
            yield " four"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=slow_stream,
            consumes="api_usd",
            cost_per_use=1.0,
            cost_per_token=0.1,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="count")

        checkpoint = _make_checkpoint(budget_mgr, budget=10.0)
        initial_usage = checkpoint.capabilities["api_usd"].current_usage

        runner = AgentRunner(gate, budget_mgr)
        task = await runner.run_as_task(agent, checkpoint)
        await started.wait()

        runner.preempt("BUDGET_EXHAUSTED")

        with pytest.raises(asyncio.CancelledError):
            await task

        # Budget accounting:
        # 1. Proxy deducted cost_per_use=1.0 before execution
        # 2. Proxy refunded cost_per_use=1.0 on BaseException
        # 3. StreamingLLMSyscall re-deducted actual: 3 tokens * 0.1 = 0.3
        actual_usage = checkpoint.capabilities["api_usd"].current_usage
        assert actual_usage == pytest.approx(initial_usage + 0.3)

    async def test_full_completion_uses_cost_per_use(self, registry, gate, budget_mgr):
        """Normal completion charges full cost_per_use, not per-token."""

        async def fast_stream(model: str, prompt: str):
            yield "a"
            yield "b"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=fast_stream,
            consumes="api_usd",
            cost_per_use=1.0,
            cost_per_token=0.1,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="done")

        checkpoint = _make_checkpoint(budget_mgr, budget=10.0)
        initial_usage = checkpoint.capabilities["api_usd"].current_usage

        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        # Normal path: cost_per_use=1.0 deducted, no refund
        actual_usage = checkpoint.capabilities["api_usd"].current_usage
        assert actual_usage == pytest.approx(initial_usage + 1.0)

    async def test_cancel_without_cost_per_token_full_refund(
        self, registry, gate, budget_mgr
    ):
        """Without cost_per_token, cancellation still does full refund."""
        started = asyncio.Event()

        async def slow_stream(model: str, prompt: str):
            yield "partial"
            started.set()
            await asyncio.sleep(10)
            yield " complete"

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=slow_stream,
            consumes="api_usd",
            cost_per_use=1.0,
            # cost_per_token not set — defaults to None
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="cancel")

        checkpoint = _make_checkpoint(budget_mgr, budget=10.0)
        initial_usage = checkpoint.capabilities["api_usd"].current_usage

        runner = AgentRunner(gate, budget_mgr)
        task = await runner.run_as_task(agent, checkpoint)
        await started.wait()

        runner.preempt("HUMAN_ABORT")

        with pytest.raises(asyncio.CancelledError):
            await task

        # Full refund — no cost_per_token set, so no re-deduction
        actual_usage = checkpoint.capabilities["api_usd"].current_usage
        assert actual_usage == pytest.approx(initial_usage)

    async def test_cost_per_token_capped_at_cost_per_use(
        self, registry, gate, budget_mgr
    ):
        """Proportional cost never exceeds cost_per_use."""
        started = asyncio.Event()

        async def many_tokens_stream(model: str, prompt: str):
            for i in range(100):
                yield f"t{i} "
                if i == 99:
                    started.set()
                    await asyncio.sleep(10)

        llm = StreamingLLMSyscall(
            registry,
            stream_fn=many_tokens_stream,
            consumes="api_usd",
            cost_per_use=1.0,
            cost_per_token=0.1,  # 100 tokens * 0.1 = 10.0, but capped at 1.0
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="test", prompt="many")

        checkpoint = _make_checkpoint(budget_mgr, budget=10.0)
        initial_usage = checkpoint.capabilities["api_usd"].current_usage

        runner = AgentRunner(gate, budget_mgr)
        task = await runner.run_as_task(agent, checkpoint)
        await started.wait()

        runner.preempt("TIMEOUT")

        with pytest.raises(asyncio.CancelledError):
            await task

        # 100 tokens * 0.1 = 10.0, but capped at cost_per_use = 1.0
        actual_usage = checkpoint.capabilities["api_usd"].current_usage
        assert actual_usage == pytest.approx(initial_usage + 1.0)


# ── Backward Compatibility ──


class TestBackwardCompatibility:
    """Existing LLMSyscall (non-streaming) still works identically."""

    async def test_llmsyscall_unchanged(self, registry, gate, budget_mgr):
        called = False

        async def fake_llm(model: str, prompt: str) -> str:
            nonlocal called
            called = True
            return "complete response"

        llm = LLMSyscall(
            registry,
            call_fn=fake_llm,
            consumes="api_usd",
            cost_per_use=1.0,
        )

        async def agent(proxy: SyscallProxy) -> str:
            return await llm.infer(proxy, model="gpt-4", prompt="hello")

        checkpoint = _make_checkpoint(budget_mgr)
        runner = AgentRunner(gate, budget_mgr)
        await runner.run(agent, checkpoint)

        assert checkpoint.status == "COMPLETED"
        assert called is True
        assert checkpoint.result == "complete response"
        assert checkpoint.syscall_log[0].request["tool_name"] == "llm_inference"
