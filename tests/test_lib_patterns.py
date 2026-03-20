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


# --- react() tests ---


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
        return "THOUGHT: thinking\nACTION: noop({})"

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


# --- map_reduce() tests ---


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


# --- plan_execute() tests ---


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
        '[{"tool": "search", "args": {"query": "data"}},'
        ' {"tool": "summarize", "args": {"text": "data"}}]',
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


# --- conversation() tests ---


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


# --- supervisor() tests ---


@pytest.mark.asyncio()
async def test_supervisor(cap_mgr):
    """supervisor: LLM picks agent, spawn/join, repeat until FINISH."""
    from castor.scheduler.agent_registry import AgentRegistry

    reg = ToolRegistry()
    agent_reg = AgentRegistry()

    @castor_tool(consumes="api", cost_per_use=1.0)
    def search(query: str) -> str:
        return f"results for {query}"

    call_count = {"n": 0}
    llm_responses = [
        "DELEGATE: researcher",
        "FINISH: researcher found results for task",
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
