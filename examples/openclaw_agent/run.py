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

from castor.capability.manager import CapabilityManager
from castor.dam.registry import ToolRegistry
from castor.dam.validator import CastorDam
from castor.llm.wrapper import LLMSyscall
from castor.models.checkpoint import AgentCheckpoint
from castor.stream.hitl import HITLHandler
from castor.stream.persistence import CheckpointStore
from castor.stream.proxy import SyscallProxy
from castor.stream.runner import AgentRunner

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


def setup_kernel(
    kb_path: Path, db_path: Path
) -> tuple[
    CastorDam,
    CapabilityManager,
    AgentRunner,
    HITLHandler,
    CheckpointStore,
    LLMSyscall,
]:
    registry = ToolRegistry()
    register_tools(registry, kb_path)

    llm = LLMSyscall(
        registry,
        call_fn=fake_llm_client,
        consumes="network",
        cost_per_use=1.0,
    )

    dam = CastorDam(registry)
    cap_mgr = CapabilityManager()
    runner = AgentRunner(dam, cap_mgr)
    hitl = HITLHandler()
    store = CheckpointStore(f"sqlite:///{db_path}")

    return dam, cap_mgr, runner, hitl, store, llm


def create_checkpoint(cap_mgr: CapabilityManager) -> AgentCheckpoint:
    caps = cap_mgr.create_capabilities({"network": 50.0, "disk": 20.0})
    return AgentCheckpoint(
        pid="openclaw-001",
        status="RUNNING",
        agent_function_name="openclaw_agent",
        capabilities=caps,
    )


# ── Interactive HITL handler ──


async def handle_hitl(
    checkpoint: AgentCheckpoint,
    dam: CastorDam,
    cap_mgr: CapabilityManager,
    hitl: HITLHandler,
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
        await hitl.approve(checkpoint, dam, cap_mgr)
        print("-> Approved.")
    elif choice == "r":
        feedback = input("Rejection reason: ").strip()
        hitl.reject(checkpoint, feedback or "Rejected by user.")
        print("-> Rejected.")
    elif choice == "m":
        feedback = input("Modification feedback: ").strip()
        hitl.modify(checkpoint, feedback or "Please modify.")
        print("-> Modified.")
    else:
        print("-> Unknown choice, defaulting to reject.")
        hitl.reject(checkpoint, "Unknown input — rejecting for safety.")


# ── Main ──


async def main() -> None:
    kb_path = Path("./openclaw_knowledge_base")
    db_path = Path("./openclaw.db")

    dam, cap_mgr, runner, hitl, store, llm = setup_kernel(kb_path, db_path)
    checkpoint = create_checkpoint(cap_mgr)

    print("=== OpenClaw Agent (powered by Castor) ===\n")
    print(f"Knowledge base: {kb_path.resolve()}")
    print(f"Budgets: network={50.0}, disk={20.0}\n")

    # The agent function needs the LLM instance — wrap it in a closure.
    async def agent_fn(proxy: SyscallProxy) -> str:
        return await openclaw_agent(proxy, llm)

    # Run loop: execute → handle HITL → resume, until done.
    while True:
        result = await runner.run(agent_fn, checkpoint)

        if result.status == "SUSPENDED_FOR_HITL":
            store.save(result)
            await handle_hitl(result, dam, cap_mgr, hitl)
            # Create a new runner for replay-based resume.
            runner = AgentRunner(dam, cap_mgr)
            continue

        # Agent completed or failed.
        break

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

    store.save(result)
    print(f"\nCheckpoint saved to {db_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
