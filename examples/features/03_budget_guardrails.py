"""Budget Guardrails

Set a budget. The agent stops itself when it runs out. No surprise bills.

Usage:
    uv run python examples/features/03_budget_guardrails.py
"""

import asyncio

from castor import Castor, castor_tool
from castor.models.preemption import PreemptedError


def _h(s: str) -> None:
    print(f"\n\033[36m{'─' * 60}\n  {s}\n{'─' * 60}\033[0m")


def _budget_bar(label: str, used: float, total: float) -> str:
    pct = used / total if total > 0 else 0
    filled = int(pct * 20)
    bar = "█" * filled + "░" * (20 - filled)
    return f"  {label:6s} [{bar}] {used:.1f}/{total:.1f}"


# ── 1. Define tools (with cost metadata for budget tracking) ──


@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> str:
    return f"[Results for '{query}': ...paper1, paper2, paper3...]"


@castor_tool(consumes="llm", cost_per_use=2.0)
async def summarize(data: str) -> str:
    return f"Summary of {data[:40]}..."


# ── 2. Define a loop agent that may run out of budget ──

TOPICS = [
    "climate change",
    "quantum computing",
    "gene therapy",
    "fusion energy",
    "space mining",
]


async def research_loop() -> str:
    from castor.lib import budget, tool

    completed: list[str] = []

    for topic in TOPICS:
        # Search (costs 1.0 api)
        try:
            result = await tool("search", query=topic)
        except PreemptedError:
            print("    search    \033[31mBUDGET EXHAUSTED\033[0m")
            break
        api_remaining = budget("api")
        print(f"    search    api remaining: {api_remaining:.1f}")

        # Summarize (costs 2.0 llm)
        try:
            result = await tool("summarize", data=str(result))
        except PreemptedError:
            llm_remaining = budget("llm")
            print(
                f"    summarize \033[31mBUDGET EXHAUSTED "
                f"(need 2.0, have {llm_remaining:.1f})\033[0m"
            )
            completed.append(f"{topic}: searched but NOT summarized")
            break
        print(f"    summarize llm remaining: {budget('llm'):.1f}")
        completed.append(f"{topic}: {result}")

    return f"Completed {len(completed)}/{len(TOPICS)} topics"


# ── 3. Run it ──


async def main() -> None:
    kernel = Castor(tools=[search, summarize])
    budgets = {"api": 3.0, "llm": 5.0}

    _h(f"Research Agent (Budget: api={budgets['api']}, llm={budgets['llm']})")
    print(f"  Plan: {len(TOPICS)} cycles of search (1.0 api) + summarize (2.0 llm)")
    print()

    # Tight budgets: enough for ~2.5 cycles of search+summarize
    checkpoint = await kernel.run(
        research_loop,
        budgets=budgets,
        pid="budget-001",
    )

    # Summary
    _h("Budget Summary")
    for name in checkpoint.capabilities:
        used = checkpoint.budget_used(name)
        total = used + checkpoint.budget_remaining(name)
        pct = (used / total * 100) if total > 0 else 0
        print(f"  {name}: {_budget_bar(name, used, total)}  ({pct:.0f}% used)")
    print(f"\n  Status: \033[32m{checkpoint.status}\033[0m")
    print(f"  Result: {checkpoint.result}")
    print(f"  Syscalls executed: {len(checkpoint.syscall_log)}")


if __name__ == "__main__":
    asyncio.run(main())
