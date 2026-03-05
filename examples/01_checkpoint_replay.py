"""Demo 01 — Checkpoint/Replay: Your agent survives process restarts.

Kill the process. Restart. The agent picks up exactly where it left off.
No re-execution of completed tools. Deterministic replay from syscall log.

Run:
    uv run python examples/01_checkpoint_replay.py
"""

import asyncio
import tempfile

from castor import (
    AgentCheckpoint,
    AgentRunner,
    CapabilityManager,
    CastorDam,
    CheckpointStore,
    HITLHandler,
    SyscallProxy,
    castor_tool,
)
from castor.dam.registry import ToolRegistry

# ── Output helpers ──


def _h(text: str) -> None:
    print(f"\n\033[1;36m=== {text} ===\033[0m")


def _live(text: str) -> None:
    print(f"  \033[34m[LIVE]\033[0m {text}")


def _replay(text: str) -> None:
    print(f"  \033[90m[REPLAY]\033[0m {text}")


def _ok(text: str) -> None:
    print(f"  \033[32m[OK]\033[0m {text}")


# ── 1. Register tools ──

registry = ToolRegistry()
call_log: list[str] = []  # tracks which tools actually execute


@castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
async def web_search(query: str) -> list[str]:
    call_log.append("web_search")
    return [f"Result for '{query}'"]


@castor_tool(consumes="api", cost_per_use=1.0, registry=registry)
async def analyze(data: list[str]) -> str:
    call_log.append("analyze")
    return f"Analysis of {len(data)} items: deploy to staging recommended"


@castor_tool(
    consumes="api", cost_per_use=2.0,
    destructive=True, requires_hitl=True, registry=registry,
)
async def deploy_code(target: str) -> str:
    call_log.append("deploy_code")
    return f"Deployed to {target}"


@castor_tool(consumes="api", cost_per_use=0.5, registry=registry)
async def notify_team(message: str) -> str:
    call_log.append("notify_team")
    return f"Team notified: {message}"


# ── 2. Define agent ──


async def deploy_agent(proxy: SyscallProxy) -> str:
    results = await proxy.syscall("web_search", {"query": "deployment best practices"})
    analysis = await proxy.syscall("analyze", {"data": results})
    deploy_result = await proxy.syscall("deploy_code", {"target": "staging"})
    await proxy.syscall("notify_team", {"message": deploy_result})
    return f"Done: {analysis}"


# ── 3. Run it ──


async def main() -> None:
    dam = CastorDam(registry)
    cap_mgr = CapabilityManager()

    with tempfile.TemporaryDirectory() as tmp:
        store = CheckpointStore(f"sqlite:///{tmp}/demo.db")

        # --- Run 1: agent executes until HITL suspend ---
        _h("Run 1: Agent executing")
        caps = cap_mgr.create_capabilities({"api": 20.0})
        checkpoint = AgentCheckpoint(
            pid="deploy-001", status="RUNNING",
            agent_function_name="deploy_agent", capabilities=caps,
        )
        runner1 = AgentRunner(dam, cap_mgr)
        checkpoint = await runner1.run(deploy_agent, checkpoint)

        for record in checkpoint.syscall_log:
            _live(f'{record.request["tool_name"]}(...) -> {record.response!r:.60s}')

        print(f"\n  Status: \033[33m{checkpoint.status}\033[0m")
        print(f"  Pending: {checkpoint.pending_hitl['tool_name']}"
              f"({checkpoint.pending_hitl['arguments']})")
        print(f"  Completed syscalls: {len(checkpoint.syscall_log)}")

        store.save(checkpoint)
        _ok("Checkpoint saved to SQLite")

        # --- Simulate process crash ---
        _h("SIMULATED PROCESS RESTART")
        print("  (New runner, loaded checkpoint from disk — proving serialization)")
        call_log.clear()

        loaded = store.load("deploy-001")
        _ok(f"Loaded checkpoint: PID={loaded.pid}, status={loaded.status}")

        # --- Human approves the destructive action ---
        _h("Human approves deployment to staging")
        handler = HITLHandler()
        await handler.approve(loaded, dam, cap_mgr)
        store.save(loaded)
        _ok(f"Status after approve: {loaded.status}")

        # --- Run 2: replay cached calls, then continue live ---
        _h("Run 2: Resuming from checkpoint")
        runner2 = AgentRunner(dam, cap_mgr)
        result = await runner2.run(deploy_agent, loaded)

        # Distinguish replayed vs live calls
        for i, record in enumerate(result.syscall_log):
            tool = record.request["tool_name"]
            resp = repr(record.response)[:60]
            if tool not in call_log:
                _replay(f"{tool}(...) -> {resp} (cached, 0ms)")
            else:
                _live(f"{tool}(...) -> {resp}")

        # --- Summary ---
        _h("Summary")
        total = len(result.syscall_log)
        live = len(call_log)
        replayed = total - live
        print(f"  Status: \033[32m{result.status}\033[0m")
        print(f"  Result: {result.result}")
        print(f"  Syscalls: {total} total ({replayed} replayed, {live} new)")
        for name, cap in result.capabilities.items():
            print(f"  Budget {name}: {cap.current_usage}/{cap.max_budget} used")


if __name__ == "__main__":
    asyncio.run(main())
