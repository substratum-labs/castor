"""Realistic tools for the live demo research assistant.

Tools are registered via ``@castor_tool`` into a *custom* registry (not the
global default) so the demo stays self-contained.

- ``web_search``: returns stubbed but realistic results (no real HTTP — keeps
  the demo fast and free of extra API keys).
- ``read_notes`` / ``save_notes``: real filesystem ops in a temp directory.
- ``send_email``: **destructive** — triggers HITL suspension.
- ``save_draft``: non-destructive fallback when the email is rejected.
"""

from __future__ import annotations

from pathlib import Path

from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry

# Stubbed search results keyed by broad topic.
_SEARCH_DB: dict[str, list[str]] = {
    "quantum computing": [
        (
            "Google Willow chip achieves 105-qubit milestone"
            " with real-time error correction (Nature, 2025)"
        ),
        (
            "IBM announces 1,121-qubit Condor processor,"
            " targets practical quantum advantage by 2026"
        ),
        (
            "Microsoft reveals topological qubit breakthrough,"
            " claims 10x longer coherence times"
        ),
        "PsiQuantum raises $450M for photonic QC at scale",
    ],
    "battery technology": [
        (
            "Toyota unveils solid-state battery with 750-mile"
            " range, 10-minute charge (2026 production)"
        ),
        (
            "QuantumScape achieves 1,000-cycle solid-state cell"
            " with 95% capacity retention"
        ),
        ("CATL announces sodium-ion battery with 200 Wh/kg energy density for EVs"),
        (
            "MIT researchers develop lithium-air battery"
            " with record 5x energy density improvement"
        ),
    ],
    "ai safety": [
        (
            "Anthropic publishes Constitutional AI 2.0"
            " framework with formal verification guarantees"
        ),
        (
            "OpenAI Superalignment team demonstrates"
            " scalable oversight for GPT-5 class models"
        ),
        (
            "DeepMind introduces RLHF-V: reward model"
            " that detects reward hacking in real time"
        ),
        (
            "EU AI Act enforcement begins: mandatory"
            " red-teaming for frontier models (Jan 2026)"
        ),
    ],
}

_FALLBACK_RESULTS = [
    "Recent academic papers show significant progress in the field (arXiv, 2025-2026)",
    "Major tech companies announce increased R&D investment in this area",
    "Open-source community contributes new benchmarks and evaluation frameworks",
    "Regulatory landscape evolving with new guidelines for responsible deployment",
]


def register_tools(registry: ToolRegistry, knowledge_base: Path) -> None:
    """Register all demo tools into *registry*."""
    knowledge_base.mkdir(parents=True, exist_ok=True)

    @castor_tool(registry=registry, consumes="api", cost_per_use=1.0)
    async def web_search(query: str) -> list[str]:
        """Search the web for recent information on a topic."""
        query_lower = query.lower()
        for key, results in _SEARCH_DB.items():
            if key in query_lower:
                return results
        return _FALLBACK_RESULTS

    @castor_tool(registry=registry, consumes="disk", cost_per_use=0.5)
    async def read_notes(topic: str) -> str:
        """Read existing notes from the local knowledge base."""
        safe_name = topic.replace(" ", "-").replace("/", "_")[:60]
        path = knowledge_base / f"{safe_name}.md"
        if path.exists():
            return path.read_text()
        return "(no existing notes)"

    @castor_tool(registry=registry, consumes="disk", cost_per_use=0.5)
    async def save_notes(topic: str, content: str) -> str:
        """Save research notes to the local knowledge base."""
        safe_name = topic.replace(" ", "-").replace("/", "_")[:60]
        path = knowledge_base / f"{safe_name}.md"
        path.write_text(content)
        return f"Saved to {path.name}"

    @castor_tool(
        registry=registry,
        consumes="api",
        cost_per_use=1.0,
        destructive=True,
        requires_hitl=True,
    )
    async def send_email(to: str, subject: str, body: str) -> str:
        """Send an email notification (requires human approval)."""
        return f"Email sent to {to}: {subject}"

    @castor_tool(registry=registry, consumes="disk", cost_per_use=0.5)
    async def save_draft(filename: str, content: str) -> str:
        """Save a draft document when email sending is rejected."""
        safe_name = filename.replace("/", "_")[:60]
        path = knowledge_base / safe_name
        path.write_text(content)
        return f"Draft saved to {path.name}"
