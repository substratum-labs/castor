"""Minimal ReAct loop — STARTER CODE, copy and modify.

This is NOT a framework feature. It's a reference implementation showing
how to write an ``agent_fn`` against the Castor kernel's syscall API.
Copy, paste, and customise for your application.

Real applications (e.g. Tiphys, castor-server) write their own agent_fn
with whatever orchestration logic they need (planner, multi-step verify,
SSE emission, MCP routing, etc.). This file is pedagogical — the
simplest possible agentic loop that demonstrates:

    1. Calling the LLM via ``proxy.syscall("llm_inference", ...)``
    2. Parsing tool-use blocks from the response
    3. Dispatching tool calls via ``proxy.syscall(<tool_name>, ...)``
    4. Feeding results back to the LLM
    5. Terminating when the LLM returns text-only (no tool calls)

All of this goes through the kernel journal — replay, fork, scan,
and budget enforcement work automatically.
"""

from __future__ import annotations

import json
from typing import Any

from castor.scheduler.proxy import SyscallProxy


async def run(
    proxy: SyscallProxy,
    *,
    model: str = "default",
    system: str = "",
    user_message: str = "",
    tools: list[dict[str, Any]] | None = None,
    max_iterations: int = 10,
) -> str:
    """One-shot ReAct loop.

    Args:
        proxy: Kernel-provided syscall gateway.
        model: Model identifier passed to ``llm_inference``.
        system: Optional system prompt.
        user_message: The user's initial message.
        tools: OpenAI-format tool specs for the LLM.
        max_iterations: Safety cap on LLM round-trips.

    Returns:
        The LLM's final text response (after all tool calls are resolved).
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    if user_message:
        messages.append({"role": "user", "content": user_message})

    for _ in range(max_iterations):
        # ── LLM call ──
        response = await proxy.syscall(
            "llm_inference",
            {"model": model, "messages": messages, "tools": tools},
        )

        content = response.get("content", [])
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        # ── No tool calls → return text ──
        if not tool_uses:
            text = " ".join(
                b["text"] for b in content if b.get("type") == "text" and b.get("text")
            )
            messages.append({"role": "assistant", "content": text})
            return text

        # ── Build assistant message with tool_calls ──
        tool_calls = [
            {
                "id": t["id"],
                "type": "function",
                "function": {
                    "name": t["name"],
                    "arguments": json.dumps(t["input"]),
                },
            }
            for t in tool_uses
        ]
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls,
            }
        )

        # ── Execute each tool call ──
        for t in tool_uses:
            result = await proxy.syscall(t["name"], t["input"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": t["id"],
                    "content": str(result),
                }
            )

    return "max_iterations reached"
