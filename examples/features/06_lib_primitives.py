"""Demo 06 — castor.lib Primitives: The new agent-developer API.

Compare with quickstart.py (Level 2 proxy API):
  - Level 2: await proxy.web_search(query="castor")
  - Level 1: await tool("web_search", query="castor")

Agent code has zero kernel imports — just `from castor.lib import ...`.

Run:
    uv run python examples/features/06_lib_primitives.py
"""

import asyncio

from castor import Castor, castor_tool
from castor.lib import budget, parallel, tool

# ── 1. Register tools ──


@castor_tool(consumes="api", cost_per_use=1.0)
async def web_search(query: str) -> list[str]:
    """Simulate a web search."""
    return [f"Result 1 for '{query}'", f"Result 2 for '{query}'"]


@castor_tool(consumes="api", cost_per_use=0.5)
async def summarize(text: str) -> str:
    """Summarize text."""
    return f"Summary: {text[:50]}..."


@castor_tool(consumes="api", cost_per_use=0.5)
async def translate(text: str, lang: str) -> str:
    """Translate text."""
    return f"[{lang}] {text}"


# ── 2. Agent using castor.lib (no proxy in signature!) ──


async def research_agent() -> str:
    """Agent using castor.lib primitives — no proxy, no kernel imports."""

    # tool() — call any registered tool by name
    results = await tool("web_search", query="castor microkernel")
    print(f"  [agent] Search: {results}")

    # parallel() — run multiple tools sequentially (future: concurrent)
    summary, translation = await parallel(
        ("summarize", {"text": str(results)}),
        ("translate", {"text": str(results), "lang": "zh"}),
    )
    print(f"  [agent] Summary: {summary}")
    print(f"  [agent] Translation: {translation}")

    # budget() — check remaining budget
    remaining = budget("api")
    print(f"  [agent] Budget remaining: {remaining}")

    return f"Done! Summary: {summary}, Translation: {translation}"


# ── 3. Run it ──


async def main() -> None:
    kernel = Castor(tools=[web_search, summarize, translate])

    print("=== castor.lib Primitives Demo ===")
    print("  Agent uses: tool(), parallel(), budget()")
    print("  No SyscallProxy in agent signature\n")

    cp = await kernel.run(research_agent, budgets={"api": 10.0})

    print(f"\n  Status: {cp.status}")
    print(f"  Result: {cp.result}")
    print(f"  Syscalls: {len(cp.syscall_log)}")
    print(f"  Budget used: {cp.budget_used('api')}/{10.0}")


if __name__ == "__main__":
    asyncio.run(main())
