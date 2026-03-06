"""Interactive CLI runner for the OpenClaw agent example.

Usage::

    uv run python examples/openclaw_agent/run.py

Demonstrates the full Castor kernel lifecycle:
1. Tool registration and LLM wrapper setup
2. Agent execution with capability budgets
3. HITL suspension and interactive approval
4. Checkpoint persistence and replay-based resume
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent import openclaw_agent
from tools import register_tools

from castor import Castor, CastorDam
from castor.dam.registry import ToolRegistry
from castor.llm.wrapper import LLMSyscall
from castor.stream.proxy import SyscallProxy

# ── Fake LLM client (replace with a real provider in production) ──

SCRIPTED_RESPONSES = [
    "Plan: 1) web_search for battery tech, 2) read existing notes, "
    "3) write summary, 4) compose Slack message, 5) send to #research.",
    "New solid-state batteries achieve 500 Wh/kg with 10-minute charging. "
    "Major breakthrough from QuantumScape and Toyota. Details in notes.",
]
_response_index = 0


async def fake_llm_client(model: str, prompt: str) -> str:
    """Simulate an LLM provider. Returns scripted responses in order."""
    global _response_index  # noqa: PLW0603
    idx = _response_index
    _response_index += 1
    if idx < len(SCRIPTED_RESPONSES):
        return SCRIPTED_RESPONSES[idx]
    return f"[LLM fallback response for: {prompt[:50]}...]"


# ── Kernel setup ──


def setup_kernel(kb_path: Path, db_path: Path) -> tuple[Castor, LLMSyscall]:
    registry = ToolRegistry()
    register_tools(registry, kb_path)

    llm = LLMSyscall(
        registry,
        call_fn=fake_llm_client,
        consumes="network",
        cost_per_use=1.0,
    )

    kernel = Castor(dam=CastorDam(registry), store=f"sqlite:///{db_path}")

    return kernel, llm


# ── Interactive HITL handler ──


async def handle_hitl(
    checkpoint: AgentCheckpoint,  # noqa: F821
    kernel: Castor,
) -> None:
    pending = checkpoint.pending_hitl
    print("\n--- HUMAN-IN-THE-LOOP REQUIRED ---")
    print(f"Tool:      {pending['tool_name']}")
    print(f"Arguments: {pending['arguments']}")
    print()
    print("  [a] Approve")
    print("  [r] Reject")
    print("  [m] Modify (provide feedback)")
    print()

    choice = input("Your choice: ").strip().lower()

    if choice == "a":
        await kernel.approve(checkpoint)
        print("-> Approved.")
    elif choice == "r":
        feedback = input("Rejection reason: ").strip()
        kernel.reject(checkpoint, feedback or "Rejected by user.")
        print("-> Rejected.")
    elif choice == "m":
        feedback = input("Modification feedback: ").strip()
        kernel.modify(checkpoint, feedback or "Please modify.")
        print("-> Modified.")
    else:
        print("-> Unknown choice, defaulting to reject.")
        kernel.reject(checkpoint, "Unknown input — rejecting for safety.")


# ── Main ──


async def main() -> None:
    kb_path = Path("./openclaw_knowledge_base")
    db_path = Path("./openclaw.db")

    kernel, llm = setup_kernel(kb_path, db_path)

    print("=== OpenClaw Agent (powered by Castor) ===\n")
    print(f"Knowledge base: {kb_path.resolve()}")
    print(f"Budgets: network={50.0}, disk={20.0}\n")

    # The agent function needs the LLM instance — wrap it in a closure.
    async def agent_fn(proxy: SyscallProxy) -> str:
        return await openclaw_agent(proxy, llm)

    # Run loop: execute → handle HITL → resume, until done.
    cp = await kernel.run(
        agent_fn, budgets={"network": 50.0, "disk": 20.0}, pid="openclaw-001"
    )

    while cp.status == "SUSPENDED_FOR_HITL":
        await kernel.save(cp)
        await handle_hitl(cp, kernel)
        # Resume via replay
        cp = await kernel.run(agent_fn, checkpoint=cp)

    result = cp

    # ── Print results ──
    print(f"\n=== Agent finished: {result.status} ===")
    print(f"Syscalls executed: {len(result.syscall_log)}")
    for i, record in enumerate(result.syscall_log):
        tool = record.request["tool_name"]
        hitl_tag = " [HITL]" if record.was_hitl else ""
        print(f"  {i + 1}. {tool}{hitl_tag}")

    print("\nBudget usage:")
    for name, cap in result.capabilities.items():
        print(f"  {name}: {cap.current_usage:.1f} / {cap.max_budget:.1f}")

    await kernel.save(result)
    print(f"\nCheckpoint saved to {db_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
