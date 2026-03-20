"""Demo 01 — Checkpoint/Replay: Your agent survives process restarts.

Kill the process. Restart. The agent picks up exactly where it left off.
No re-execution of completed tools. Deterministic replay from syscall log.

Run:
    uv run python examples/features/01_checkpoint_replay.py
"""

import asyncio
import tempfile

from castor import Castor, SyscallProxy, castor_tool

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

call_log: list[str] = []  # tracks which tools actually execute


@castor_tool(consumes="api", cost_per_use=1.0)
async def web_search(query: str) -> list[str]:
    call_log.append("web_search")
    return [f"Result for '{query}'"]


@castor_tool(consumes="api", cost_per_use=1.0)
async def analyze(data: list[str]) -> str:
    call_log.append("analyze")
    return f"Analysis of {len(data)} items: deploy to staging recommended"


@castor_tool(
    consumes="api",
    cost_per_use=2.0,
    destructive=True,
    requires_hitl=True,
)
async def deploy_code(target: str) -> str:
    call_log.append("deploy_code")
    return f"Deployed to {target}"


@castor_tool(consumes="api", cost_per_use=0.5)
async def notify_team(message: str) -> str:
    call_log.append("notify_team")
    return f"Team notified: {message}"


# ── 2. Define agent ──


async def deploy_agent(proxy: SyscallProxy) -> str:
    results = await proxy.web_search(query="deployment best practices")
    analysis = await proxy.analyze(data=results)
    deploy_result = await proxy.deploy_code(target="staging")
    await proxy.notify_team(message=deploy_result)
    return f"Done: {analysis}"


# ── 3. Run it ──


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        kernel = Castor(
            tools=[web_search, analyze, deploy_code, notify_team],
            store=f"sqlite:///{tmp}/demo.db",
        )

        # --- Run 1: agent executes until HITL suspend ---
        _h("Run 1: Agent executing")
        checkpoint = await kernel.run(
            deploy_agent,
            budgets={"api": 20.0},
            pid="deploy-001",
        )

        for record in checkpoint.syscall_log:
            _live(f"{record.request['tool_name']}(...) -> {record.response!r:.60s}")

        print(f"\n  Status: \033[33m{checkpoint.status}\033[0m")
        print(f"  Pending: {checkpoint.pending_tool}({checkpoint.pending_args})")
        print(f"  Completed syscalls: {len(checkpoint.syscall_log)}")

        await kernel.save(checkpoint)
        _ok("Checkpoint saved to SQLite")

        # --- Simulate process crash ---
        _h("SIMULATED PROCESS RESTART")
        print("  (New runner, loaded checkpoint from disk — proving serialization)")
        call_log.clear()

        loaded = kernel.load("deploy-001")
        _ok(f"Loaded checkpoint: PID={loaded.pid}, status={loaded.status}")

        # --- Human approves the destructive action ---
        _h("Human approves deployment to staging")
        await kernel.approve(loaded)
        await kernel.save(loaded)
        _ok(f"Status after approve: {loaded.status}")

        # --- Run 2: replay cached calls, then continue live ---
        _h("Run 2: Resuming from checkpoint")
        result = await kernel.run(deploy_agent, checkpoint=loaded)

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
        for name in result.capabilities:
            used = result.budget_used(name)
            total = used + result.budget_remaining(name)
            print(f"  Budget {name}: {used}/{total} used")


if __name__ == "__main__":
    asyncio.run(main())
