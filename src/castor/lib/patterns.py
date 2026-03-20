"""High-level agent patterns built on castor.lib primitives."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from castor.lib.primitives import chat, tool
from castor.lib.spawn import join, spawn


async def parallel(*tool_calls: tuple[str, dict[str, Any]]) -> list[Any]:
    """Execute multiple tool calls sequentially, return results in order.

    Each element is (tool_name, arguments_dict).
    Note: currently sequential — future versions may use spawn/join for true
    concurrency when tools support it.
    """
    results = []
    for name, args in tool_calls:
        result = await tool(name, **args)
        results.append(result)
    return results


async def react(
    goal: str,
    tools: list[str],
    *,
    max_steps: int = 10,
    tool_name: str = "llm_inference",
) -> str:
    """ReAct loop: Think -> Act -> Observe, until LLM outputs FINISH.

    The LLM is prompted to output one of:
    - ACTION: tool_name({"arg": "value"})
    - FINISH: final_answer

    Args:
        goal: The task description for the LLM.
        tools: List of tool names the LLM may use.
        max_steps: Maximum think-act-observe cycles.
        tool_name: Name of the registered LLM tool.
    """
    observations: list[str] = []
    system = (
        f"You are a ReAct agent. Available tools: {tools}\n"
        "On each step respond with EXACTLY one of:\n"
        '  ACTION: tool_name({{"arg": "value"}})\n'
        "  FINISH: your_final_answer\n"
        "Do NOT output anything else."
    )

    for step in range(max_steps):
        if observations:
            history = "\n".join(observations)
            prompt = f"Goal: {goal}\n\nHistory:\n{history}\n\nNext step:"
        else:
            prompt = f"Goal: {goal}\n\nNext step:"

        response = await chat(prompt, system=system, tool_name=tool_name)

        # Parse FINISH
        finish_match = re.search(r"FINISH:\s*(.+)", response, re.DOTALL)
        if finish_match:
            return finish_match.group(1).strip()

        # Parse ACTION
        action_match = re.search(r"ACTION:\s*(\w+)\((.+?)\)\s*$", response, re.DOTALL)
        if action_match:
            act_tool = action_match.group(1)
            try:
                act_args = json.loads(action_match.group(2))
            except json.JSONDecodeError:
                act_args = {}

            if act_tool not in tools:
                msg = f"Step {step + 1}: ERROR — tool {act_tool!r} not in allowed tools"
                observations.append(msg)
                continue

            result = await tool(act_tool, **act_args)
            observations.append(f"Step {step + 1}: {act_tool}({act_args}) -> {result}")
        else:
            msg = f"Step {step + 1}: Could not parse response: {response}"
            observations.append(msg)

    raise RuntimeError(f"react() exceeded max_steps={max_steps} without FINISH")


async def map_reduce(
    items: list[Any],
    map_tool: str,
    reduce_tool: str,
    *,
    map_args_fn: Callable[[Any], dict[str, Any]] | None = None,
    reduce_args_fn: Callable[[list[Any]], dict[str, Any]] | None = None,
) -> Any:
    """Map each item through map_tool, then reduce all results with reduce_tool.

    Args:
        items: List of items to process.
        map_tool: Tool name to apply to each item.
        reduce_tool: Tool name to aggregate results.
        map_args_fn: Converts an item to tool kwargs. Default: {"item": item}.
        reduce_args_fn: Converts result list to tool kwargs.
            Default: {"items": results}.
    """

    def _default_map(item: Any) -> dict[str, Any]:
        return {"item": item}

    def _default_reduce(results: list[Any]) -> dict[str, Any]:
        return {"items": results}

    if map_args_fn is None:
        map_args_fn = _default_map
    if reduce_args_fn is None:
        reduce_args_fn = _default_reduce

    # Map phase
    map_results = []
    for item in items:
        result = await tool(map_tool, **map_args_fn(item))
        map_results.append(result)

    # Reduce phase
    return await tool(reduce_tool, **reduce_args_fn(map_results))


async def plan_execute(
    goal: str,
    executor_tools: list[str],
    *,
    tool_name: str = "llm_inference",
) -> str:
    """Plan then execute: LLM generates a step list, then executes each step.

    The planner LLM is asked to return a JSON list of steps:
    [{"tool": "name", "args": {...}}, ...]

    After executing all steps, the LLM summarizes the results.

    Args:
        goal: The task description.
        executor_tools: List of tool names the executor may use.
        tool_name: Name of the registered LLM tool.
    """
    # Phase 1: Plan
    plan_prompt = (
        f"Goal: {goal}\n"
        f"Available tools: {executor_tools}\n"
        "Return a JSON array of steps. Each step: "
        '{"tool": "tool_name", "args": {"key": "value"}}\n'
        "Return ONLY the JSON array, nothing else."
    )
    plan_response = await chat(plan_prompt, tool_name=tool_name)

    try:
        steps = json.loads(plan_response)
    except json.JSONDecodeError:
        return f"ERROR: Could not parse plan: {plan_response}"

    # Phase 2: Execute
    step_results = []
    for i, step in enumerate(steps):
        step_tool = step.get("tool", "")
        step_args = step.get("args", {})
        if step_tool not in executor_tools:
            step_results.append(f"Step {i + 1}: SKIPPED — {step_tool!r} not allowed")
            continue
        result = await tool(step_tool, **step_args)
        step_results.append(f"Step {i + 1}: {step_tool}({step_args}) -> {result}")

    # Phase 3: Summarize
    summary_prompt = (
        f"Goal: {goal}\n"
        f"Execution results:\n" + "\n".join(step_results) + "\n"
        "Summarize the outcome. Start with FINISH:"
    )
    summary = await chat(summary_prompt, tool_name=tool_name)
    finish_match = re.search(r"FINISH:\s*(.+)", summary, re.DOTALL)
    return finish_match.group(1).strip() if finish_match else summary


async def conversation(
    system: str,
    *,
    max_turns: int = 20,
    tool_name: str = "llm_inference",
    input_tool: str = "user_input",
    exit_word: str = "EXIT",
) -> list[dict[str, str]]:
    """Multi-turn chat: user_input -> LLM -> repeat until exit_word or max_turns.

    Args:
        system: System prompt for the LLM.
        max_turns: Maximum conversation exchanges.
        tool_name: Name of the registered LLM tool.
        input_tool: Name of the tool that gets user input.
        exit_word: User input that ends the conversation.

    Returns:
        List of {"role": "user"/"assistant", "content": "..."} dicts.
    """
    history: list[dict[str, str]] = []

    for _ in range(max_turns):
        user_msg = await tool(input_tool)
        if user_msg == exit_word:
            break

        history.append({"role": "user", "content": str(user_msg)})

        # Build prompt from history
        prompt = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        response = await chat(prompt, system=system, tool_name=tool_name)
        history.append({"role": "assistant", "content": response})

    return history


async def supervisor(
    task: str,
    agents: list[str],
    *,
    tool_name: str = "llm_inference",
    max_rounds: int = 5,
) -> str:
    """Supervisor pattern: LLM decides which agent to delegate to.

    The LLM outputs one of:
    - DELEGATE: agent_name
    - FINISH: final_answer

    Args:
        task: The task description.
        agents: List of available agent names.
        tool_name: Name of the registered LLM tool.
        max_rounds: Maximum delegation rounds.
    """
    results: list[str] = []
    system = (
        f"You are a supervisor. Available agents: {agents}\n"
        "On each round respond with EXACTLY one of:\n"
        "  DELEGATE: agent_name\n"
        "  FINISH: your_final_answer\n"
    )

    for round_num in range(max_rounds):
        if results:
            history = "\n".join(results)
            prompt = f"Task: {task}\n\nAgent results so far:\n{history}\n\nNext action:"
        else:
            prompt = f"Task: {task}\n\nNext action:"

        response = await chat(prompt, system=system, tool_name=tool_name)

        # Parse FINISH
        finish_match = re.search(r"FINISH:\s*(.+)", response, re.DOTALL)
        if finish_match:
            return finish_match.group(1).strip()

        # Parse DELEGATE
        delegate_match = re.search(r"DELEGATE:\s*(\w+)", response)
        if delegate_match:
            agent_name = delegate_match.group(1)
            if agent_name not in agents:
                msg = (
                    f"Round {round_num + 1}: ERROR — agent {agent_name!r} not available"
                )
                results.append(msg)
                continue
            handle = await spawn(agent_name)
            agent_result = await join(handle)
            results.append(f"Round {round_num + 1}: {agent_name} -> {agent_result}")
        else:
            results.append(f"Round {round_num + 1}: Could not parse: {response}")

    raise RuntimeError(f"supervisor() exceeded max_rounds={max_rounds} without FINISH")
