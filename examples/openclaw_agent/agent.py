"""OpenClaw agent function — a personal AI assistant built on Castor.

Demonstrates:
- LLM reasoning via ``LLMSyscall`` (replay-safe)
- Safe tool calls (web search, note read/write)
- Destructive tool calls that trigger HITL suspension (send_message)
- Graceful handling of HITL rejection (fallback to writing a note)
"""

from __future__ import annotations

from typing import Any

from castor.llm.wrapper import LLMSyscall
from castor.stream.proxy import SyscallProxy


async def openclaw_agent(proxy: SyscallProxy, llm: LLMSyscall) -> str:
    """A personal assistant that researches a topic and notifies the user.

    Steps:
    1. LLM plans which tools to use
    2. Web search to gather information
    3. Read existing notes for context
    4. Write a summary note
    5. LLM composes a notification message
    6. Send message (destructive — triggers HITL)
    7. Return summary

    If the message send is rejected by the human, the agent falls back to
    saving a draft note instead.
    """
    # Step 1: LLM decides on a research plan (response used for logging)
    await llm.infer(
        proxy,
        model="gpt-4",
        prompt="User asked: 'Research the latest on battery technology and "
        "send me a summary on Slack.' Plan the steps.",
    )

    # Step 2: Search the web
    search_results = await proxy.web_search(
        query="battery technology 2026 breakthroughs"
    )

    # Step 3: Check existing notes for context
    existing = await proxy.read_note(filename="battery-tech.md")

    # Step 4: Save research findings as a note
    findings = _format_findings(search_results, existing)
    await proxy.write_note(filename="battery-tech.md", content=findings)

    # Step 5: LLM composes a message
    message = await llm.infer(
        proxy,
        model="gpt-4",
        prompt=f"Compose a Slack message summarising: {findings}",
    )

    # Step 6: Send the message (destructive — will suspend for HITL)
    send_result = await proxy.send_message(
        platform="slack", recipient="#research", body=message
    )

    # Step 7: Handle rejection gracefully
    if _is_rejected(send_result):
        await proxy.write_note(
            filename="battery-tech-draft.md", content=f"DRAFT: {message}"
        )
        return "Message rejected — saved as draft note."

    return f"Research complete. {send_result}"


def _format_findings(search_results: Any, existing_note: Any) -> str:
    """Combine search results and existing notes into a markdown summary."""
    lines = ["# Battery Technology Research\n"]
    if isinstance(existing_note, str) and "not found" not in existing_note.lower():
        lines.append(f"## Previous Notes\n{existing_note}\n")
    lines.append("## New Findings\n")
    if isinstance(search_results, list):
        for r in search_results:
            lines.append(f"- {r}")
    return "\n".join(lines)


def _is_rejected(result: Any) -> bool:
    """Check if a syscall result indicates HITL rejection."""
    if hasattr(result, "rejected"):
        return result.rejected
    return isinstance(result, dict) and result.get("status") == "HITL_REJECTED"
