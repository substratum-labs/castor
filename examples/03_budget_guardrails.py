"""Demo 03 — Budget Guardrails: Your agent cannot overspend.

Level 2 (SyscallProxy) — uses the raw proxy API for full kernel control.
See examples 06-08 for Level 1 (castor.lib) equivalents.

Set a budget. The agent stops itself when it runs out. No surprise bills.
Shows automatic cost enforcement with graceful degradation.

Run:
    uv run python examples/03_budget_guardrails.py
"""

import asyncio

from castor import Castor, SyscallProxy, castor_tool

# ── Output helpers ──

_BAR_WIDTH = 20


def _h(text: str) -> None:
    print(f"\n\033[1;36m=== {text} ===\033[0m")


def _budget_bar(label: str, used: float, total: float) -> str:
    ratio = min(used / total, 1.0) if total > 0 else 0
    filled = int(ratio * _BAR_WIDTH)
    bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
    color = "\033[32m" if ratio < 0.8 else ("\033[33m" if ratio < 1.0 else "\033[31m")
    return f"{color}[{bar}]\033[0m {used:.1f}/{total:.1f}"


# ── 1. Register tools ──


@castor_tool(consumes="api", cost_per_use=1.0)
async def search(query: str) -> list[str]:
    return [f"Result for '{query}'"]


@castor_tool(consumes="llm", cost_per_use=2.0)
async def summarize(data: str) -> str:
    return f"Summary of: {data[:40]}..."


# ── 2. Define a loop agent that may run out of budget ──

TOPICS = [
    "climate change",
    "quantum computing",
    "gene therapy",
    "fusion energy",
    "space mining",
]


async def research_loop(proxy: SyscallProxy) -> str:
    completed: list[str] = []

    for topic in TOPICS:
        # Search
        result = await proxy.search(query=topic)
        is_exhausted = (
            isinstance(result, dict)
            and result.get("status") == "INSUFFICIENT_CAPABILITY"
        )
        if is_exhausted:
            print("    search    \033[31mINSUFFICIENT\033[0m")
            break
        api_used = proxy.checkpoint.budget_used("api")
        api_total = api_used + proxy.budget("api")
        print(f"    search    api: {_budget_bar('api', api_used, api_total)}")

        # Summarize
        result = await proxy.summarize(data=str(result))
        is_exhausted = (
            isinstance(result, dict)
            and result.get("status") == "INSUFFICIENT_CAPABILITY"
        )
        if is_exhausted:
            remain = proxy.budget("llm")
            print(
                f"    summarize \033[31mINSUFFICIENT "
                f"(need 2.0, have {remain:.1f})\033[0m"
            )
            completed.append(f"{topic}: searched but NOT summarized")
            break
        llm_used = proxy.checkpoint.budget_used("llm")
        llm_total = llm_used + proxy.budget("llm")
        print(f"    summarize llm: {_budget_bar('llm', llm_used, llm_total)}")
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
