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
    """Full pipeline: Castor() -> new-style agent -> castor.lib calls."""
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
