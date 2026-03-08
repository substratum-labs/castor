"""Castor Python baseline benchmarks.

Measures key performance metrics for comparison with the Rust/PyO3 port.
Run: uv run python benchmarks/bench_baseline.py
"""

from __future__ import annotations

import asyncio
import statistics
import time

from castor.capability.manager import CapabilityManager
from castor.gate.decorator import castor_tool
from castor.gate.registry import ToolRegistry
from castor.gate.validator import SyscallGate
from castor.models.checkpoint import (
    AgentCheckpoint,
    CastorMessage,
    SyscallRecord,
)
from castor.stream.persistence import CheckpointStore
from castor.stream.proxy import SyscallProxy

# ── Setup ──

registry = ToolRegistry()


@castor_tool(consumes="test", cost_per_use=0.0, registry=registry)
def noop_tool(value: str) -> str:
    return value


gate = SyscallGate(registry)
cap_mgr = CapabilityManager()


def make_checkpoint(
    num_syscalls: int = 0,
) -> AgentCheckpoint:
    caps = cap_mgr.create_capabilities({"test": 1_000_000.0})
    log = [
        SyscallRecord(
            request={
                "tool_name": "noop_tool",
                "arguments": {"value": f"v{i}"},
            },
            response=f"v{i}",
        )
        for i in range(num_syscalls)
    ]
    return AgentCheckpoint(
        pid="bench-001",
        status="RUNNING",
        agent_function_name="bench",
        capabilities=caps,
        syscall_log=log,
    )


# ── Benchmark helpers ──

ITERATIONS = 10_000


def bench_sync(name: str, fn, iterations: int = ITERATIONS):
    """Benchmark a synchronous function."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        elapsed = time.perf_counter_ns() - start
        times.append(elapsed)
    report(name, times)


async def bench_async(name: str, fn, iterations: int = ITERATIONS):
    """Benchmark an async function."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        await fn()
        elapsed = time.perf_counter_ns() - start
        times.append(elapsed)
    report(name, times)


def report(name: str, times_ns: list[int]):
    """Print p50/p95/p99 for a benchmark."""
    times_us = [t / 1000 for t in times_ns]
    p50 = statistics.median(times_us)
    p95 = statistics.quantiles(times_us, n=20)[18]  # 95th
    p99 = statistics.quantiles(times_us, n=100)[98]  # 99th
    n = len(times_us)
    print(f"  {name:<45} {p50:>8.1f} {p95:>8.1f} {p99:>8.1f}  (n={n})")


# ── Benchmarks ──


async def bench_syscall_fast_path():
    """Proxy.syscall() → execute tool (non-destructive, no replay)."""

    async def fn():
        cp = make_checkpoint(0)
        proxy = SyscallProxy(cp, gate, cap_mgr)
        await proxy.syscall("noop_tool", {"value": "x"})

    await bench_async("syscall fast path", fn)


async def bench_syscall_replay_path():
    """Proxy.syscall() → serve from syscall_log cache."""

    async def fn():
        cp = make_checkpoint(1)
        proxy = SyscallProxy(cp, gate, cap_mgr)
        await proxy.syscall("noop_tool", {"value": "v0"})

    await bench_async("syscall replay path", fn)


def bench_gate_validation():
    """SyscallGate.validate() -- Pydantic schema validation."""

    def fn():
        gate.validate("noop_tool", {"value": "hello"})

    bench_sync("gate validation", fn)


def bench_checkpoint_serialization():
    """model_dump_json() at various syscall log sizes."""
    for size in (10, 100, 1000):
        cp = make_checkpoint(size)

        def fn(c=cp):
            c.model_dump_json()

        bench_sync(f"checkpoint serialize ({size} syscalls)", fn, iterations=1000)


def bench_checkpoint_persistence():
    """SQLite write round-trip at various sizes."""
    import tempfile

    for size in (10, 100, 1000):
        cp = make_checkpoint(size)
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(f"sqlite:///{tmp}/bench.db")

            def fn(c=cp, s=store):
                s.save(c)

            bench_sync(
                f"checkpoint persist ({size} syscalls)",
                fn,
                iterations=1000,
            )


def bench_budget_operations():
    """Deduct/refund cycle."""

    def fn():
        caps = cap_mgr.create_capabilities({"test": 1000.0})
        cap_mgr.deduct(caps, "test", 1.0)
        cap_mgr.refund(caps, "test", 1.0)

    bench_sync("budget deduct/refund cycle", fn)


def bench_lodge_token_counting():
    """Token counting + FIFO eviction victim selection."""
    from castor.mmu.token_counter import CharCountEstimator

    counter = CharCountEstimator()
    msgs = [
        CastorMessage(role="user", content=f"message number {i}") for i in range(50)
    ]

    def fn():
        total = 0
        for msg in msgs:
            total += counter.count(msg.content)
        # Simulate FIFO victim selection (unpinned messages)
        victims = [m for m in msgs if not m.pinned][:10]
        return len(victims)

    bench_sync("lodge token count (50 msgs)", fn, iterations=5000)


# ── Main ──


async def main():
    print("=" * 80)
    print("Castor Python Baseline Benchmarks")
    print("=" * 80)
    print()
    print(f"  {'Benchmark':<45} {'p50 µs':>8} {'p95 µs':>8} {'p99 µs':>8}")
    print("  " + "-" * 75)

    await bench_syscall_fast_path()
    await bench_syscall_replay_path()
    bench_gate_validation()
    bench_checkpoint_serialization()
    bench_checkpoint_persistence()
    bench_budget_operations()
    bench_lodge_token_counting()

    print()
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
