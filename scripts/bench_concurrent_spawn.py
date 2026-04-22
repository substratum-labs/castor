"""Concurrent spawn scheduling verification.

§4 (S4) from BRIEFING_CASTOR_SCHEDULING.md.

Measurement only — no fix. Spawns N children, each doing 50 noop
syscalls, and checks for starvation, fairness, and deadlocks.

Usage:
    cd ~/projects/castor
    uv run python scripts/bench_concurrent_spawn.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path
from typing import Any

NOOP_PER_CHILD = 50


async def bench_spawn(n_children: int) -> dict[str, Any]:
    """Spawn N children, each doing NOOP_PER_CHILD syscalls."""
    from castor import AgentRegistry, Castor, castor_tool

    @castor_tool(consumes="api", cost_per_use=0.0)
    async def noop() -> str:
        return "ok"

    child_times: dict[str, float] = {}

    async def child_agent(proxy) -> str:
        pid = proxy.checkpoint.pid
        t0 = time.perf_counter()
        for _ in range(NOOP_PER_CHILD):
            await proxy.syscall("noop", {})
        child_times[pid] = time.perf_counter() - t0
        return pid

    reg = AgentRegistry()
    reg.register("child", child_agent)

    kernel = Castor(
        tools=[noop],
        agent_registry=reg,
        budgets={"api": 100000.0},
    )

    async def parent(proxy):
        # Spawn all children (sync — one at a time, sequentially)
        results = []
        for i in range(n_children):
            r = await proxy.syscall(
                "spawn_agent",
                {
                    "agent_name": "child",
                    "capabilities": {"api": 10000.0},
                    "priority": 5,
                },
            )
            results.append(r)
        return results

    t_start = time.perf_counter()
    cp = await kernel.run(parent)
    wall_time = time.perf_counter() - t_start

    times = list(child_times.values())
    if not times:
        return {
            "n": n_children,
            "wall_time": wall_time,
            "error": "no children completed",
        }

    # Single-child baseline (first child's time)
    baseline = times[0] if times else wall_time

    return {
        "n": n_children,
        "wall_time": wall_time,
        "min": min(times),
        "max": max(times),
        "median": statistics.median(times),
        "mean": statistics.mean(times),
        "stddev": statistics.stdev(times) if len(times) > 1 else 0.0,
        "baseline": baseline,
        "ratio": wall_time / baseline if baseline > 0 else 0,
        "status": cp.status,
        "children_completed": len(times),
    }


def _format(seconds: float) -> str:
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}µs"
    if seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.2f}s"


async def main() -> None:
    sizes = [2, 5, 10, 20]

    print("=" * 60)
    print("  Concurrent Spawn Verification")
    print(f"  {NOOP_PER_CHILD} noop syscalls per child")
    print("=" * 60)

    # True single-child baseline (N=1)
    print("\n  Baseline (N=1) ...", end=" ", flush=True)
    baseline_r = await bench_spawn(1)
    true_baseline = baseline_r["wall_time"]
    print(f"wall={_format(true_baseline)}")

    results: list[dict[str, Any]] = []
    for n in sizes:
        print(f"\n  N={n} ...", end=" ", flush=True)
        try:
            r = await asyncio.wait_for(bench_spawn(n), timeout=30.0)
        except TimeoutError:
            r = {"n": n, "error": "DEADLOCK — timed out after 30s"}
            print("❌ DEADLOCK")
        else:
            if "error" in r:
                print(f"❌ {r['error']}")
            else:
                print(f"✅ wall={_format(r['wall_time'])}")
        results.append(r)

    # Report
    print("\n" + "=" * 60)
    print("  Results")
    print("=" * 60)

    headers = [
        "N",
        "wall",
        "min",
        "max",
        "median",
        "stddev",
        "max/median",
        "wall/baseline",
    ]
    rows: list[list[str]] = []
    all_pass = True

    for r in results:
        if "error" in r:
            rows.append([str(r["n"]), r["error"]] + ["—"] * 6)
            all_pass = False
            continue

        # Use true baseline for ratio
        ratio = r["wall_time"] / true_baseline if true_baseline > 0 else 0
        ratio_max_med = r["max"] / r["median"] if r["median"] > 0 else 0

        # For sequential spawn, wall ≈ N × baseline is expected.
        # Fail if overhead is > 2x beyond linear (ratio > N * 2),
        # or if any child starves (max > 10x median).
        starved = ratio_max_med > 10
        excessive_overhead = ratio > r["n"] * 2

        if starved or excessive_overhead:
            all_pass = False

        rows.append(
            [
                str(r["n"]),
                _format(r["wall_time"]),
                _format(r["min"]),
                _format(r["max"]),
                _format(r["median"]),
                _format(r["stddev"]),
                f"{ratio_max_med:.1f}x{'❌' if starved else ''}",
                f"{ratio:.1f}x (expect ~{r['n']}x)"
                + ("❌" if excessive_overhead else ""),
            ]
        )

    # Print table
    widths = [
        max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)
    ]
    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    print("\n| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)) + " |")
    print(sep)
    for row in rows:
        print("| " + " | ".join(c.ljust(w) for c, w in zip(row, widths)) + " |")

    # Pass criteria
    print(f"\n  Baseline (N=1): {_format(true_baseline)}")
    print(f"  Overall: {'✅ PASS' if all_pass else '❌ FAIL'}")
    print(
        "  Criteria: wall < N*2x baseline (sequential), max < 10x median, no deadlocks"
    )

    # Write report
    report_path = Path("benchmarks/concurrent_spawn_2026-04.md")
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(_gen_report(results, all_pass, true_baseline))
    print(f"\n  Report written to {report_path}")


def _gen_report(results: list[dict], all_pass: bool, baseline: float = 0) -> str:
    lines = [
        "# Concurrent Spawn Verification — 2026-04",
        "",
        "> Generated by `scripts/bench_concurrent_spawn.py`.",
        f"> {NOOP_PER_CHILD} noop syscalls per child, sync spawn.",
        "> Machine-specific — regenerate locally.",
        "",
        "## Results",
        "",
        "| N | wall | min | max | median | stddev | max/median | wall/baseline |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['n']} | {r['error']} | — | — | — | — | — | — |")
            continue
        mm_ratio = r["max"] / r["median"] if r["median"] > 0 else 0
        wall_ratio = r["wall_time"] / baseline if baseline > 0 else 0
        lines.append(
            f"| {r['n']} | {_format(r['wall_time'])} | "
            f"{_format(r['min'])} | {_format(r['max'])} | "
            f"{_format(r['median'])} | {_format(r['stddev'])} | "
            f"{mm_ratio:.1f}x | {wall_ratio:.1f}x (~{r['n']}x) |"
        )
    lines.extend(
        [
            "",
            f"## Verdict: {'✅ PASS' if all_pass else '❌ FAIL'}",
            "",
            f"Baseline (N=1): {_format(baseline)}",
            "",
            "Criteria:",
            "- Wall time scales linearly with N (ratio ≈ N, within 2x)",
            "- No child starves (max < 10x median)",
            "- No deadlocks (test completes in bounded time)",
            "",
            "## Notes",
            "",
            "Current scheduler is sequential sync spawn — children run one "
            "at a time. Wall time scales linearly with N (expected). No "
            "starvation or deadlocks possible in this model since there is "
            "no concurrency. Async spawn with true parallelism would show "
            "different characteristics.",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
