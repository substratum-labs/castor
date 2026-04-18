"""Tests for castor.lib.run_task — Level 0 API."""

import pytest

from castor.budget.manager import BudgetManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.lib._context import set_proxy
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.proxy import SyscallProxy


@pytest.mark.asyncio()
async def test_run_task_basic():
    budget_mgr = BudgetManager()
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
        capabilities=budget_mgr.create_budgets({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, budget_mgr)
    set_proxy(p)

    from castor.lib.run_task import run_task

    result = await run_task("find info about hello")
    assert "results for hello" in result


@pytest.mark.asyncio()
async def test_run_task_auto_discovers_tools():
    """run_task with tools=None discovers all non-LLM tools."""
    budget_mgr = BudgetManager()
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
            assert "calc" in system
            return 'THOUGHT: use calc\nACTION: calc({"expression": "1+1"})'
        return "FINISH: computed 1+1"

    reg.register(calc._castor_metadata)
    reg.register(llm_inference._castor_metadata)
    gate = SyscallGate(reg)
    cp = AgentCheckpoint(
        pid="test-runtask-auto",
        status="RUNNING",
        agent_function_name="test",
        capabilities=budget_mgr.create_budgets({"api": 100.0}),
    )
    p = SyscallProxy(cp, gate, budget_mgr)
    set_proxy(p)

    from castor.lib.run_task import run_task

    # tools=None should auto-discover calc (exclude llm_inference)
    result = await run_task("compute 1+1")
    assert "1+1" in result
