"""Demo 05 — Multi-Agent: The full Castor experience.

A coordinator spawns specialist agents, delegates budgets, handles
human approvals across the hierarchy, and produces a final report.

Run:
    uv run python examples/05_hero_multi_agent.py
"""

import asyncio

from castor import (
    Castor,
    SyscallProxy,
    castor_agent,
    castor_tool,
)

# ── Output helpers ──


def _h(text: str) -> None:
    print(f"\n\033[1;36m=== {text} ===\033[0m")


def _ok(text: str) -> None:
    print(f"  \033[32m[OK]\033[0m {text}")


def _warn(text: str) -> None:
    print(f"  \033[33m[!!]\033[0m {text}")


def _replay(text: str) -> None:
    print(f"  \033[90m[REPLAY]\033[0m {text}")


# ── 1. Register tools ──


@castor_tool(consumes="network", cost_per_use=1.0)
async def web_search(query: str) -> list[str]:
    return [f"Finding 1 for '{query}'", f"Finding 2 for '{query}'"]


@castor_tool(consumes="disk", cost_per_use=0.5)
async def write_note(filename: str, content: str) -> str:
    return f"Saved {filename} ({len(content)} chars)"


@castor_tool(consumes="disk", cost_per_use=0.5)
async def read_note(filename: str) -> str:
    return "Finding 1 for 'AI safety': important results"


@castor_tool(
    consumes="network",
    cost_per_use=2.0,
    destructive=True,
    requires_hitl=True,
)
async def send_message(platform: str, channel: str, body: str) -> str:
    return f"Message sent to {platform}#{channel}"


# ── 2. Register agents (auto-collected by default_agent_registry) ──


@castor_agent(name="researcher")
async def researcher(proxy: SyscallProxy) -> str:
    results = await proxy.web_search(query="AI safety 2026")
    await proxy.write_note(
        filename="findings.md",
        content=f"Research findings: {results}",
    )
    return f"Research complete: {len(results)} findings"


@castor_agent(name="publisher")
async def publisher(proxy: SyscallProxy) -> str:
    note = await proxy.read_note(filename="findings.md")
    result = await proxy.send_message(
        platform="slack",
        channel="#research",
        body=f"Summary: {note[:50]}",
    )
    return f"Published: {result}"


# ── 3. Define coordinator agent ──


async def coordinator(proxy: SyscallProxy) -> str:
    # Spawn two children with delegated budgets
    print("  Spawning child agents...")
    caps = {"network": 5.0, "disk": 3.0}
    handle_a = await proxy.spawn("researcher", capabilities=caps)
    _ok(f"researcher spawned (handle={handle_a})")

    handle_b = await proxy.spawn("publisher", capabilities=caps)
    _ok(f"publisher spawned (handle={handle_b})")

    # Join — waits for children to complete (or suspends if child needs HITL)
    print("\n  Joining children...")
    result_a = await proxy.join(handle_a)
    _ok(f"researcher: {result_a}")

    result_b = await proxy.join(handle_b)
    _ok(f"publisher: {result_b}")

    return f"Coordinated: [{result_a}] + [{result_b}]"


# ── 4. Run it ──


async def main() -> None:
    kernel = Castor(tools=[web_search, write_note, read_note, send_message])

    _h("Castor Multi-Agent Demo")
    print("  Coordinator PID: coord-001")
    print("  Budget: network=20.0, disk=10.0")

    # --- Run 1: coordinator spawns children, publisher hits HITL ---
    cp = await kernel.run(
        coordinator,
        budgets={"network": 20.0, "disk": 10.0},
        pid="coord-001",
    )

    if cp.is_suspended:
        _h("HITL Propagation")
        _warn("Child suspended -> Parent suspended")
        _warn(f"Pending: {cp.pending_tool}()")

        # Inspect child checkpoint
        last_record = cp.syscall_log[-1]
        child_cp = last_record.child_checkpoint
        if child_cp and child_cp.is_suspended:
            print(f"  Child PID: {child_cp.pid}")
            print(f"  Child tool: {child_cp.pending_tool}")
            print(f"  Child args: {child_cp.pending_args}")

        # Human approves the child's action through the parent
        _h("Human approves child's message send")
        await kernel.approve(cp)

        if cp.status == "RUNNING":
            _ok("Child completed, parent unblocked")

            # --- Run 2: replay everything, continue coordinator ---
            _h("Resuming coordinator (full replay)")
            cp = await kernel.run(coordinator, checkpoint=cp)

    # ── Agent Tree ──
    _h("Agent Tree")

    # Parent
    def _fmt(c, res: str) -> str:
        u = c.budget_used(res)
        return f"{res}={u:.1f}/{u + c.budget_remaining(res):.1f}"

    print(
        f"  {cp.pid} (coordinator)"
        f"  {cp.status}"
        f"  {_fmt(cp, 'network')}"
        f"  {_fmt(cp, 'disk')}"
    )

    # Children (from syscall log)
    for record in cp.syscall_log:
        if record.child_checkpoint:
            child = record.child_checkpoint
            parts = []
            if "network" in child.capabilities:
                parts.append(_fmt(child, "network"))
            if "disk" in child.capabilities:
                parts.append(_fmt(child, "disk"))
            extra = "  ".join(parts)
            print(f"    +-- {child.pid}  {child.status}  {extra}")

    # ── Final Result ──
    _h("Final Result")
    print(f"  Status: \033[32m{cp.status}\033[0m")
    print(f"  Result: {cp.result}")
    print(f"  Total syscalls (coordinator): {len(cp.syscall_log)}")


if __name__ == "__main__":
    asyncio.run(main())
