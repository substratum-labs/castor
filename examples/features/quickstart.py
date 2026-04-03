"""Castor quickstart — minimal example showing tool registration, budget, and HITL.

Run:
    uv run python examples/features/quickstart.py
"""

import asyncio

from castor import Castor
from castor.lib import tool

# ── 1. Define tools (plain functions, no decorators) ──


async def web_search(query: str) -> list[str]:
    """Simulate a web search."""
    return [f"Result 1 for '{query}'", f"Result 2 for '{query}'"]


def delete_files(paths: list[str]) -> int:
    """Delete files (destructive — requires human approval)."""
    print(f"  [tool] Would delete: {paths}")
    return len(paths)


# ── 2. Define an agent function ──


async def research_agent() -> str:
    """An agent that searches the web and tries to clean up temp files."""
    results = await tool("web_search", query="castor kernel")
    print(f"  [agent] Search returned: {results}")

    # This will suspend for human approval (destructive tool):
    deleted = await tool("delete_files", paths=["/tmp/old.log"])
    return f"Done! Cleaned {deleted} files."


# ── 3. Run it ──


async def main() -> None:
    kernel = Castor(
        tools=[web_search, delete_files],
        destructive=["delete_files"],
        budgets={"api": 10.0},
    )

    # First run — will suspend at delete_files
    print("=== Run 1: agent executes until HITL suspend ===")
    cp = await kernel.run(research_agent, pid="quickstart-001")
    print(f"  Suspended: {cp.is_suspended}")
    print(f"  Pending: {cp.pending_tool}({cp.pending_args})")

    # Human approves the destructive operation
    print("\n=== Human approves the delete ===")
    await kernel.approve(cp)
    print(f"  Status after approve: {cp.status}")

    # Resume — replays past syscalls, continues from where it left off
    print("\n=== Run 2: agent resumes after approval ===")
    cp = await kernel.run(research_agent, checkpoint=cp)
    print(f"  Status: {cp.status}")
    print(f"  Result: {cp.result}")

    # Budget check
    print("\n=== Budget usage ===")
    for name in cp.capabilities:
        used = cp.budget_used(name)
        remaining = cp.budget_remaining(name)
        print(f"  {name}: {used}/{used + remaining} used")


if __name__ == "__main__":
    asyncio.run(main())
