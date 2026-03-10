"""OpenClaw agent tools — registered with Castor's @castor_tool decorator.

Five tools spanning safe, destructive, and HITL-required categories:

| Tool          | Resource | Cost | Destructive | HITL |
|---------------|----------|------|-------------|------|
| web_search    | network  | 1.0  | no          | no   |
| read_note     | disk     | 0.5  | no          | no   |
| write_note    | disk     | 1.0  | no          | no   |
| delete_note   | disk     | 1.0  | yes         | yes  |
| send_message  | network  | 2.0  | yes         | yes  |
"""

from __future__ import annotations

from pathlib import Path

from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry


def register_tools(registry: ToolRegistry, knowledge_base: Path) -> None:
    """Register all OpenClaw tools into the given registry.

    ``knowledge_base`` is the directory where markdown notes are stored.
    """
    knowledge_base.mkdir(parents=True, exist_ok=True)

    # ── Safe tools ──

    @castor_tool(consumes="network", cost_per_use=1.0, registry=registry)
    def web_search(query: str) -> list[str]:
        """Search the web and return a list of result snippets."""
        # Stub — in production this would call a search API.
        return [
            f"[1] Wikipedia: {query}",
            f"[2] Blog post about {query}",
            f"[3] Research paper on {query}",
        ]

    @castor_tool(consumes="disk", cost_per_use=0.5, registry=registry)
    def read_note(filename: str) -> str:
        """Read a markdown note from the knowledge base."""
        path = knowledge_base / filename
        if not path.exists():
            return f"Note '{filename}' not found."
        return path.read_text()

    @castor_tool(consumes="disk", cost_per_use=1.0, registry=registry)
    def write_note(filename: str, content: str) -> str:
        """Write or update a markdown note in the knowledge base."""
        path = knowledge_base / filename
        path.write_text(content)
        return f"Saved '{filename}' ({len(content)} chars)."

    # ── Destructive / HITL tools ──

    @castor_tool(
        consumes="disk",
        cost_per_use=1.0,
        destructive=True,
        requires_hitl=True,
        registry=registry,
    )
    def delete_note(filename: str) -> str:
        """Delete a note from the knowledge base (requires human approval)."""
        path = knowledge_base / filename
        if not path.exists():
            return f"Note '{filename}' not found."
        path.unlink()
        return f"Deleted '{filename}'."

    @castor_tool(
        consumes="network",
        cost_per_use=2.0,
        destructive=True,
        requires_hitl=True,
        registry=registry,
    )
    def send_message(platform: str, recipient: str, body: str) -> str:
        """Send a message to an external platform (requires human approval)."""
        # Stub — in production this would call Slack/Telegram/etc.
        return f"Message sent to {recipient} on {platform}."
