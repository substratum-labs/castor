"""Demo 08 — Multi-Agent with castor.lib: spawn/join and supervisor pattern.

Compare with 05_hero_multi_agent.py (proxy-based):
  - Level 2: await proxy.spawn("researcher", capabilities=caps)
  - Level 1: await spawn("researcher", capabilities=caps)

Run:
    uv run python examples/08_lib_multi_agent.py
"""

import asyncio

from castor import Castor, castor_agent, castor_tool
from castor.lib import join, spawn, tool

# ── 1. Register tools ──


@castor_tool(consumes="api", cost_per_use=1.0)
async def web_search(query: str) -> list[str]:
    """Search the web."""
    return [f"Result for '{query}'"]


@castor_tool(consumes="api", cost_per_use=0.5)
async def write_note(filename: str, content: str) -> str:
    """Write a note to storage."""
    return f"Saved {filename}"


@castor_tool(consumes="api", cost_per_use=0.5)
async def read_note(filename: str) -> str:
    """Read a note from storage."""
    return "AI safety research findings: promising progress"


@castor_tool(consumes="api", cost_per_use=1.0)
async def send_report(recipient: str, body: str) -> str:
    """Send a report."""
    return f"Report sent to {recipient}"


# ── 2. Child agents using castor.lib ──


@castor_agent(name="researcher")
async def researcher() -> str:
    """Research agent — uses tool() from castor.lib."""
    results = await tool("web_search", query="AI safety 2026")
    await tool("write_note", filename="findings.md", content=str(results))
    print(f"  [researcher] Found {len(results)} results")
    return f"Research done: {len(results)} findings"


@castor_agent(name="reporter")
async def reporter() -> str:
    """Reporter agent — reads notes and sends report."""
    note = await tool("read_note", filename="findings.md")
    await tool("send_report", recipient="team@example.com", body=note)
    print("  [reporter] Report sent")
    return "Report delivered"


# ── 3. Coordinator using spawn/join from castor.lib ──


async def coordinator() -> str:
    """Coordinator — spawns children via castor.lib (no proxy needed)."""
    caps = {"api": 5.0}

    print("  [coordinator] Spawning researcher...")
    h1 = await spawn("researcher", capabilities=caps)

    print("  [coordinator] Spawning reporter...")
    h2 = await spawn("reporter", capabilities=caps)

    print("  [coordinator] Joining...")
    r1 = await join(h1)
    r2 = await join(h2)

    return f"All done: [{r1}] + [{r2}]"


# ── 4. Run it ──


async def main() -> None:
    kernel = Castor(
        tools=[web_search, write_note, read_note, send_report],
    )

    print("=== castor.lib Multi-Agent Demo ===")
    print("  Coordinator spawns researcher + reporter")
    print("  All agents use castor.lib (no proxy in signature)\n")

    cp = await kernel.run(
        coordinator,
        budgets={"api": 20.0},
        pid="lib-coord-001",
    )

    # Show agent tree
    print(f"\n  Status: {cp.status}")
    print(f"  Result: {cp.result}")
    print(f"  Coordinator syscalls: {len(cp.syscall_log)}")

    for record in cp.syscall_log:
        if record.child_checkpoint:
            child = record.child_checkpoint
            used = child.budget_used("api")
            total = used + child.budget_remaining("api")
            print(f"    +-- {child.pid}  {child.status}  api={used:.1f}/{total:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
