"""Real LLM + Interactive HITL Demo.

A two-act demonstration of Castor's core capabilities using a **real** LLM
provider (via LiteLLM) and a Rich-based interactive terminal UI.

Act 1 — Live Research:
    The agent calls a real LLM to plan and compose, executes tools, and
    triggers HITL approval for a destructive ``send_email`` action.

Act 2 — Crash Recovery:
    The same agent re-runs from the saved checkpoint.  Every syscall
    (including LLM calls) is served from the replay cache — zero real
    API calls, zero cost, identical result.

Usage::

    # Install demo dependencies
    uv sync --extra demo

    # Run with Anthropic Claude
    ANTHROPIC_API_KEY=sk-... uv run python examples/live_demo/run.py

    # Run with OpenAI
    OPENAI_API_KEY=sk-... uv run python examples/live_demo/run.py --model gpt-4o

    # Custom topic
    uv run python examples/live_demo/run.py "battery technology breakthroughs"

    # Override model via env var
    CASTOR_MODEL=gemini/gemini-pro uv run python examples/live_demo/run.py
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
import tempfile
from pathlib import Path

import litellm
from agent import research_agent
from tools import register_tools
from ui import (
    console,
    print_act_header,
    print_budget_summary,
    print_comparison,
    print_step,
    rich_interactive,
)

from castor import Castor, SyscallGate
from castor.gate.registry import ToolRegistry
from castor.llm.wrapper import LLMSyscall
from castor.scheduler.proxy import SyscallProxy

# Suppress litellm's verbose logging by default.
litellm.suppress_debug_info = True

DEFAULT_MODEL = "anthropic/claude-sonnet-4-5-20250929"
DEFAULT_TOPIC = "quantum computing breakthroughs"


# ─────────────────────────────────────────────────────────────────────
# LLM wrapper
# ─────────────────────────────────────────────────────────────────────


async def call_llm(model: str, messages: list[dict]) -> str:
    """Call an LLM provider via LiteLLM (supports 100+ models)."""
    response = await litellm.acompletion(model=model, messages=messages)
    return response.choices[0].message.content


# ─────────────────────────────────────────────────────────────────────
# Kernel setup
# ─────────────────────────────────────────────────────────────────────


def setup_kernel(
    knowledge_base: Path,
) -> tuple[Castor, LLMSyscall]:
    """Create a Castor kernel with all tools + LLM registered."""
    registry = ToolRegistry()
    register_tools(registry, knowledge_base)

    llm = LLMSyscall(
        registry,
        call_fn=call_llm,
        consumes="api",
        cost_per_use=2.0,
        tool_name="llm_inference",
    )

    kernel = Castor(gate=SyscallGate(registry), structured_results=True)
    return kernel, llm


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Castor Live Demo — Real LLM + Interactive HITL",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        default=DEFAULT_TOPIC,
        help=f"Research topic (default: {DEFAULT_TOPIC!r})",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"LiteLLM model ID (default: {DEFAULT_MODEL}, or CASTOR_MODEL env var)",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


async def main() -> None:
    import os

    args = parse_args()
    topic: str = args.topic
    model: str = args.model or os.environ.get("CASTOR_MODEL", DEFAULT_MODEL)

    with tempfile.TemporaryDirectory(prefix="castor_demo_") as tmpdir:
        kb_path = Path(tmpdir) / "knowledge_base"
        kernel, llm = setup_kernel(kb_path)

        # Build agent closure — captures llm, topic, model.
        async def agent_fn(proxy: SyscallProxy) -> str:
            return await research_agent(proxy, llm, topic, model)

        # ── Banner ──
        console.print()
        console.rule("[bold bright_blue]Castor Live Demo[/bold bright_blue]")
        console.print(f"  Topic:  [bold]{topic}[/bold]")
        console.print(f"  Model:  [bold]{model}[/bold]")
        console.print("  Budget: api=10.0, disk=10.0")
        console.print()

        # ════════════════════════════════════════════════════════════
        # Act 1: Live Research with Real LLM
        # ════════════════════════════════════════════════════════════
        print_act_header(1, "Live Research with Real LLM")

        try:
            cp = await kernel.run_until_complete(
                agent_fn,
                budgets={"api": 10.0, "disk": 10.0},
                on_hitl=rich_interactive,
                pid="research-001",
            )
        except Exception as exc:
            console.print(f"\n  [bold red]Error:[/bold red] {exc}")
            console.print(
                "  Hint: ensure your API key is set "
                "(e.g. ANTHROPIC_API_KEY=sk-... or OPENAI_API_KEY=sk-...)\n"
            )
            sys.exit(1)

        # Show what happened
        console.print()
        console.print(f"  [bold]Agent status:[/bold] {cp.status}")
        console.print(f"  [bold]Result:[/bold] {cp.result}")
        console.print()

        # Show execution log
        for i, record in enumerate(cp.syscall_log, 1):
            print_step(
                i,
                record.request.get("tool_name", "?"),
                record.response,
                is_replay=False,
            )

        console.print()
        print_budget_summary(cp)

        # ════════════════════════════════════════════════════════════
        # Act 2: Crash Recovery (zero API calls)
        # ════════════════════════════════════════════════════════════
        console.print()
        console.print(
            "  [dim]Press Enter to simulate crash recovery "
            "(re-run from checkpoint, zero API calls)...[/dim]"
        )
        input()

        print_act_header(2, "Crash Recovery — Zero API Calls")

        # Deep-copy the checkpoint to simulate loading from persistence.
        saved_cp = copy.deepcopy(cp)
        # Reset status to RUNNING so the runner replays from the log.
        saved_cp.status = "RUNNING"
        saved_cp.result = None

        cp2 = await kernel.run(agent_fn, checkpoint=saved_cp)

        console.print(f"  [bold]Agent status:[/bold] {cp2.status}")
        console.print(f"  [bold]Result:[/bold] {cp2.result}")
        console.print()

        # All calls were replayed
        for i, record in enumerate(cp2.syscall_log, 1):
            print_step(
                i,
                record.request.get("tool_name", "?"),
                record.response,
                is_replay=True,
            )

        console.print()
        print_comparison(cp, cp2)
        console.rule("[bold bright_blue]Demo Complete[/bold bright_blue]")
        console.print()


if __name__ == "__main__":
    asyncio.run(main())
