"""Research assistant agent powered by a real LLM.

The agent follows a fixed pipeline:

1. **Plan** — ask the LLM to plan research steps.
2. **Search** — call ``web_search`` to gather data.
3. **Context** — call ``read_notes`` for existing knowledge.
4. **Save** — persist combined findings via ``save_notes``.
5. **Compose** — ask the LLM to write an email summary.
6. **Send** — call ``send_email`` (destructive → HITL approval).
7. **Fallback** — if rejected, save a draft instead.

All LLM calls go through ``castor.lib.tool("llm_inference", ...)`` so responses
are cached in the ``syscall_log`` and replayed deterministically on resume.
"""

from __future__ import annotations

from typing import Any

from castor.lib import tool

SYSTEM_PROMPT = """\
You are a research assistant. You help users research topics and compose \
concise, professional email summaries of your findings.
Keep your responses short and to the point — no more than 2-3 paragraphs.
"""


async def research_agent(topic: str, model: str) -> str:
    """Run the full research-to-email pipeline.

    Parameters
    ----------
    topic:
        Free-text research topic provided by the user.
    model:
        LiteLLM model identifier (e.g. ``"anthropic/claude-sonnet-4-5-20250929"``).
    """
    # ── Step 1: LLM plans research strategy ──
    plan = await tool(
        "llm_inference",
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"I need you to research: '{topic}'.\n"
                    "Briefly describe your plan (2-3 sentences)."
                ),
            },
        ],
    )

    # ── Step 2: Gather data ──
    search_results = await tool("web_search", query=topic)

    # ── Step 3: Read existing context ──
    existing_notes = await tool("read_notes", topic=topic)

    # ── Step 4: Save combined findings ──
    combined = _format_findings(topic, search_results, existing_notes, plan)
    await tool("save_notes", topic=topic, content=combined)

    # ── Step 5: LLM composes email ──
    email_body = await tool(
        "llm_inference",
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Based on the following research findings, compose a short "
                    f"email summary (2-3 paragraphs) to send to the team.\n\n"
                    f"Topic: {topic}\n\n"
                    f"Findings:\n{combined}"
                ),
            },
        ],
    )

    # ── Step 6: Send email (HITL) ──
    result = await tool(
        "send_email",
        to="team@company.com",
        subject=f"Research Summary: {topic}",
        body=email_body,
    )

    # ── Step 7: Handle rejection ──
    if _is_rejected(result):
        await tool(
            "save_draft",
            filename=f"{topic.replace(' ', '-')}-draft.md",
            content=f"# Draft: {topic}\n\n{email_body}",
        )
        return "Email rejected by reviewer — saved as draft."

    return "Research complete. Email sent to team@company.com."


# ── Helpers ──


def _format_findings(
    topic: str,
    search_results: Any,
    existing_notes: Any,
    plan: Any,
) -> str:
    """Combine all sources into a structured markdown document."""
    lines = [f"# Research: {topic}\n"]

    lines.append("## Research Plan\n")
    lines.append(str(plan) + "\n")

    if isinstance(existing_notes, str) and "(no existing notes)" not in existing_notes:
        lines.append("## Previous Notes\n")
        lines.append(existing_notes + "\n")

    lines.append("## New Findings\n")
    if isinstance(search_results, list):
        for item in search_results:
            lines.append(f"- {item}")
    else:
        lines.append(str(search_results))

    return "\n".join(lines)


def _is_rejected(result: Any) -> bool:
    """Check if a syscall result indicates HITL rejection."""
    if hasattr(result, "rejected"):
        return result.rejected
    return isinstance(result, dict) and result.get("status") == "HITL_REJECTED"
