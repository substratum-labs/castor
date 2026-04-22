"""Memory performance benchmark — journal + cold storage + end-to-end.

§3 (M6) from BRIEFING_CASTOR_SCHEDULING.md.

Measures:
  1. Journal: append, get, scan_from latency at N = [1k, 10k, 100k]
  2. ColdStorage: store, search latency at N = [1k, 10k, 100k]
  3. End-to-end: mem_write + mem_search through proxy syscall stack

Usage:
    cd ~/projects/castor
    uv run python scripts/bench_memory.py
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

WARMUP = 200  # iterations discarded before measurement


def _percentile(data: list[float], p: int) -> float:
    """Sorted-input percentile."""
    if not data:
        return 0.0
    data = sorted(data)
    k = (len(data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(data) - 1)
    return data[f] + (data[c] - data[f]) * (k - f)


def _us(s: float) -> str:
    return f"{s * 1_000_000:.1f}µs"


def _ms(s: float) -> str:
    return f"{s * 1000:.2f}ms"


# ── 1. Journal ──


def bench_journal(sizes: list[int]) -> list[dict[str, Any]]:
    from castor.kernel.journal import InMemoryJournal
    from castor.models.checkpoint import SyscallRecord

    results = []
    for n in sizes:
        journal = InMemoryJournal([])

        # Warm-up
        for i in range(WARMUP):
            journal.append(
                SyscallRecord(
                    request={"tool_name": "warmup", "arguments": {"i": i}},
                    response="w",
                )
            )

        # Measure append (post-warmup)
        append_times: list[float] = []
        for i in range(n):
            rec = SyscallRecord(
                request={"tool_name": "noop", "arguments": {"i": i}},
                response=f"r_{i}",
            )
            t0 = time.perf_counter()
            journal.append(rec)
            append_times.append(time.perf_counter() - t0)

        # Measure get (random access at full size)
        total_len = len(journal)
        indices = [random.randint(0, total_len - 1) for _ in range(1000)]
        get_times: list[float] = []
        for idx in indices:
            t0 = time.perf_counter()
            journal.get(idx)
            get_times.append(time.perf_counter() - t0)

        # Measure scan
        t0 = time.perf_counter()
        for _ in journal.scan_from(0):
            pass
        scan_time = time.perf_counter() - t0

        results.append(
            {
                "n": n,
                "append_p50": _percentile(append_times, 50),
                "append_p99": _percentile(append_times, 99),
                "get_p99": _percentile(get_times, 99),
                "scan_total": scan_time,
            }
        )
    return results


# ── 2. ColdStorage ──


async def bench_cold_storage(sizes: list[int]) -> list[dict[str, Any]]:
    from castor.mmu.cold_storage import InMemoryColdStorage
    from castor.models.checkpoint import CastorMessage

    results = []
    for n in sizes:
        cold = InMemoryColdStorage()

        # Warm-up
        for i in range(WARMUP):
            await cold.store(
                "bench",
                [CastorMessage(id=f"wu-{i}", role="u", content=f"warmup {i}")],
            )

        # Measure store
        store_times: list[float] = []
        for i in range(n):
            msg = CastorMessage(
                id=f"msg-{i}",
                role="user",
                content=f"message {i} about topic {i % 100}",
            )
            t0 = time.perf_counter()
            await cold.store("bench", [msg])
            store_times.append(time.perf_counter() - t0)

        # Measure search at full size
        queries = [f"topic {i % 100}" for i in range(100)]
        search_times: list[float] = []
        for q in queries:
            t0 = time.perf_counter()
            await cold.search("bench", q, limit=5)
            search_times.append(time.perf_counter() - t0)

        results.append(
            {
                "n": n,
                "store_p50": _percentile(store_times, 50),
                "store_p99": _percentile(store_times, 99),
                "search_p99": _percentile(search_times, 99),
            }
        )
    return results


# ── 3. End-to-end ──


async def bench_end_to_end(
    prefill: int = 5000, measure_ops: int = 200
) -> dict[str, Any]:
    """Measure syscall latency at steady state (after prefilling journal)."""
    from castor import Castor, castor_tool
    from castor.mmu.cold_storage import InMemoryColdStorage

    cold = InMemoryColdStorage()

    @castor_tool(consumes="api", cost_per_use=0.0)
    async def noop() -> str:
        return "ok"

    kernel = Castor(tools=[noop], cold_storage=cold, agent_id="bench-e2e")

    write_times: list[float] = []
    search_times: list[float] = []

    async def agent(proxy):
        # Prefill journal to simulate steady state
        for i in range(prefill):
            await proxy.syscall(
                "mem_write", {"content": f"prefill {i}", "role": "user"}
            )

        # Measure writes at steady state
        for i in range(measure_ops):
            t0 = time.perf_counter()
            await proxy.syscall(
                "mem_write", {"content": f"measured {i}", "role": "user"}
            )
            write_times.append(time.perf_counter() - t0)

        # Measure searches at steady state
        for i in range(min(measure_ops, 50)):
            t0 = time.perf_counter()
            await proxy.syscall("mem_search", {"query": f"measured {i}", "limit": 5})
            search_times.append(time.perf_counter() - t0)

        return "done"

    await kernel.run(agent)

    return {
        "prefill": prefill,
        "n_writes": measure_ops,
        "n_searches": min(measure_ops, 50),
        "write_p50": _percentile(write_times, 50),
        "write_p99": _percentile(write_times, 99),
        "search_p50": _percentile(search_times, 50),
        "search_p99": _percentile(search_times, 99),
    }


# ── Report ──


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    hdr = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)) + " |"
    body = [
        "| " + " | ".join(c.ljust(w) for c, w in zip(row, widths)) + " |"
        for row in rows
    ]
    return "\n".join([hdr, sep, *body])


async def main() -> None:
    sizes = [1_000, 10_000, 100_000]

    print("=" * 60)
    print("  Memory Performance Benchmark")
    print(f"  Warmup: {WARMUP} iterations discarded")
    print("=" * 60)

    # 1. Journal
    print("\n### 1. Journal (InMemoryJournal)")
    j_results = bench_journal(sizes)
    rows = [
        [
            f"{r['n']:,}",
            _us(r["append_p50"]),
            _us(r["append_p99"]),
            _us(r["get_p99"]),
            _ms(r["scan_total"]),
        ]
        for r in j_results
    ]
    print(_table(["N", "append p50", "append p99", "get p99", "scan_from(0)"], rows))
    last = j_results[-1]
    ap = last["append_p99"] < 0.001
    print(
        f"\n  append p99 at 100k: {_us(last['append_p99'])}"
        f" — {'✅ PASS' if ap else '❌ FAIL'} (< 1ms)"
    )

    # 2. ColdStorage
    print("\n### 2. ColdStorage (InMemoryColdStorage)")
    c_results = await bench_cold_storage(sizes)
    rows = [
        [f"{r['n']:,}", _us(r["store_p50"]), _us(r["store_p99"]), _ms(r["search_p99"])]
        for r in c_results
    ]
    print(_table(["N", "store p50", "store p99", "search p99"], rows))
    # Check at ALL sizes
    sp_10k = c_results[1]["search_p99"] < 0.1
    sp_100k = c_results[2]["search_p99"] < 1.0  # 1s at 100k
    sp = sp_10k and sp_100k
    print(
        f"\n  search p99 at 10k: {_ms(c_results[1]['search_p99'])}"
        f" — {'✅' if sp_10k else '❌'} (< 100ms)"
    )
    print(
        f"  search p99 at 100k: {_ms(c_results[2]['search_p99'])}"
        f" — {'✅' if sp_100k else '❌'} (< 1s)"
    )

    # 3. End-to-end (at steady state with 5k prefill)
    print("\n### 3. End-to-end (proxy.syscall, 5k prefill)")
    e2e = await bench_end_to_end(prefill=5000, measure_ops=200)
    rows = [
        [
            "mem_write",
            str(e2e["n_writes"]),
            _us(e2e["write_p50"]),
            _us(e2e["write_p99"]),
        ],
        [
            "mem_search",
            str(e2e["n_searches"]),
            _us(e2e["search_p50"]),
            _us(e2e["search_p99"]),
        ],
    ]
    print(_table(["syscall", "N", "p50", "p99"], rows))
    op = e2e["write_p99"] < 0.0002
    print(
        f"\n  write p99: {_us(e2e['write_p99'])}"
        f" — {'✅ PASS' if op else '⚠️ NOTE'} (target < 200µs)"
    )

    # Write report (not committed — generated artifact)
    report_path = Path("benchmarks/memory_perf_2026-04.md")
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(_gen_report(j_results, c_results, e2e, ap, sp, op))
    print(f"\n  Report written to {report_path}")


def _gen_report(j, c, e2e, ap, sp, op) -> str:
    lines = [
        "# Memory Performance Benchmark — 2026-04",
        "",
        f"> Generated by `scripts/bench_memory.py`. Warmup: {WARMUP} iters.",
        "> Machine-specific — do not commit; regenerate locally.",
        "",
        "## 1. Journal (InMemoryJournal)",
        "",
        "| N | append p50 | append p99 | get p99 | scan_from(0) |",
        "|---|---|---|---|---|",
    ]
    for r in j:
        lines.append(
            f"| {r['n']:,} | {_us(r['append_p50'])} | "
            f"{_us(r['append_p99'])} | {_us(r['get_p99'])} | "
            f"{_ms(r['scan_total'])} |"
        )
    v = "✅ PASS" if ap else "❌ FAIL"
    lines.append(f"\nappend p99 at 100k: {v} (< 1ms)")
    lines.extend(
        [
            "",
            "## 2. ColdStorage (InMemoryColdStorage)",
            "",
            "| N | store p50 | store p99 | search p99 |",
            "|---|---|---|---|",
        ]
    )
    for r in c:
        lines.append(
            f"| {r['n']:,} | {_us(r['store_p50'])} | "
            f"{_us(r['store_p99'])} | {_ms(r['search_p99'])} |"
        )
    v = "✅ PASS" if sp else "❌ FAIL"
    lines.append(f"\nsearch: {v} (< 100ms at 10k, < 1s at 100k)")
    lines.extend(
        [
            "",
            f"## 3. End-to-end (proxy.syscall, {e2e['prefill']} prefill)",
            "",
            "| syscall | N | p50 | p99 |",
            "|---|---|---|---|",
            f"| mem_write | {e2e['n_writes']} | {_us(e2e['write_p50'])}"
            f" | {_us(e2e['write_p99'])} |",
            f"| mem_search | {e2e['n_searches']} | {_us(e2e['search_p50'])}"
            f" | {_us(e2e['search_p99'])} |",
        ]
    )
    v = "✅ PASS" if op else "⚠️ NOTE"
    lines.append(f"\nwrite p99: {v} (target < 200µs)")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Journal append/get are O(1). scan_from is O(N).",
            "",
            "ColdStorage search is O(N) brute-force in InMemory backend.",
            "Production backends (SQLiteVec, ChromaDB) use vector indexing.",
            "",
            "End-to-end overhead includes journal write, budget check, "
            "invocation_id, and post-syscall effects. Measured at steady "
            f"state after {e2e['prefill']} prefill operations.",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
