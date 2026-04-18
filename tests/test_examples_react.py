"""Tests for the reference ReAct example (castor.examples.react).

Verifies that the minimal loop:
1. Calls llm_inference → parses tool_use → dispatches tool → loops
2. Terminates when the LLM returns text-only
3. Journal records all syscalls in the expected order
"""

from __future__ import annotations

import pytest

from castor import Castor


@pytest.mark.asyncio
async def test_react_loop_completes():
    """Full ReAct loop: LLM returns tool call, then text."""
    call_count = 0

    async def fake_llm(
        model: str = "", messages: list | None = None, tools: list | None = None
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: return a tool_use
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "echo",
                        "input": {"text": "hello"},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        # Second call: return text (terminal)
        return {
            "content": [{"type": "text", "text": "The echo said: hello"}],
            "usage": {"input_tokens": 15, "output_tokens": 8},
        }

    async def echo(text: str = "") -> str:
        return f"echo: {text}"

    kernel = Castor(tools=[echo], llm=fake_llm)

    async def agent(proxy):
        from castor.examples.react import run

        return await run(
            proxy,
            model="test",
            user_message="say hello",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "echo text",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    },
                }
            ],
        )

    cp = await kernel.run(agent)
    assert cp.status == "COMPLETED"
    assert "echo said" in cp.result

    # Journal should have: llm_inference, echo, llm_inference (3 syscalls)
    tool_names = [r.request.get("tool_name") for r in cp.syscall_log]
    assert tool_names == ["llm_inference", "echo", "llm_inference"]


@pytest.mark.asyncio
async def test_react_loop_no_tools():
    """LLM returns text immediately — no tool dispatch."""

    async def fake_llm(
        model: str = "", messages: list | None = None, tools: list | None = None
    ):
        return {
            "content": [{"type": "text", "text": "Just text, no tools"}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }

    async def noop() -> str:
        return ""

    kernel = Castor(tools=[noop], llm=fake_llm)

    async def agent(proxy):
        from castor.examples.react import run

        return await run(proxy, model="test", user_message="hi")

    cp = await kernel.run(agent)
    assert cp.status == "COMPLETED"
    assert cp.result == "Just text, no tools"
    assert len(cp.syscall_log) == 1  # single llm_inference


@pytest.mark.asyncio
async def test_import_works():
    """from castor.examples.react import run"""
    from castor.examples.react import run

    assert callable(run)
