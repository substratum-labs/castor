"""run_task: Level 0 API — one-sentence goal, auto ReAct execution."""

from __future__ import annotations

from castor.lib._context import get_proxy
from castor.lib.patterns import react


async def run_task(
    goal: str,
    *,
    tools: list[str] | None = None,
    max_steps: int = 10,
    tool_name: str = "llm_inference",
) -> str:
    """Level 0 API: describe a goal, get a result.

    Wraps react() with automatic tool discovery.

    Args:
        goal: Natural language description of the task.
        tools: Explicit tool list. None = auto-discover from Gate.
        max_steps: Maximum ReAct steps.
        tool_name: Name of the registered LLM tool.

    Raises:
        RuntimeError: If no LLM tool is registered or max_steps exceeded.
    """
    if tools is None:
        proxy = get_proxy()
        all_tools = proxy._gate.list_tools()
        tools = [t for t in all_tools if t != tool_name]

    if not tools:
        raise RuntimeError("run_task() requires at least one non-LLM tool registered")

    return await react(goal, tools=tools, max_steps=max_steps, tool_name=tool_name)
