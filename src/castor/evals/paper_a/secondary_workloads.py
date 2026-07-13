"""Secondary Paper A workloads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from castor import Castor, SyscallGate, castor_tool
from castor.evals.actuator_bench import ActuatorBench
from castor.gate.registry import ToolRegistry
from castor.models.preemption import PreemptedError
from castor.scheduler.hitl import HITLHandler


@dataclass(frozen=True)
class SecondaryWorkloadResult:
    """Observed outcome of a secondary Paper A workload."""

    workload: str
    decision: str
    checkpoint_status: str
    committed_effects: int
    dup_commits: int
    journal_statuses: tuple[str, ...]


def run_s_loop_workload() -> SecondaryWorkloadResult:
    """Run two unit-cost model calls under a one-call request budget."""
    return asyncio.run(_run_s_loop_workload())


async def _run_s_loop_workload() -> SecondaryWorkloadResult:
    with TemporaryDirectory() as temp_dir:
        actuator = ActuatorBench(Path(temp_dir) / "actuator.sqlite3")
        registry = ToolRegistry()

        @castor_tool(consumes="requests", cost_per_use=1.0, registry=registry)
        def call_model(operation_id: str = "") -> dict[str, str]:
            return dict(actuator.commit("model_call", {}, operation_id=operation_id))

        kernel = Castor(gate=SyscallGate(registry))

        async def agent(proxy) -> dict[str, object]:
            first = await proxy.syscall("call_model", {})
            try:
                second = await proxy.syscall("call_model", {})
            except PreemptedError:
                return {"first": first}
            return {"first": first, "second": second}

        checkpoint = await kernel.run(agent, budgets={"requests": 1.0})
        metrics = actuator.metrics(expected_effects=1)
        journal_statuses = tuple(
            record.response.get("status", "COMMITTED")
            if isinstance(record.response, dict)
            else "COMPLETED"
            for record in checkpoint.syscall_log
        )
        return SecondaryWorkloadResult(
            workload="S-Loop",
            decision="budget_stop",
            checkpoint_status=checkpoint.status,
            committed_effects=metrics.committed_effects,
            dup_commits=metrics.dup_commits,
            journal_statuses=journal_statuses,
        )


def run_s_hitl_workload(
    work_dir: Path, *, decision: Literal["approve", "reject"]
) -> SecondaryWorkloadResult:
    """Run a payment that requires explicit human approval or rejection."""
    return asyncio.run(_run_s_hitl_workload(work_dir, decision=decision))


async def _run_s_hitl_workload(
    work_dir: Path, *, decision: Literal["approve", "reject"]
) -> SecondaryWorkloadResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    actuator = ActuatorBench(work_dir / "actuator.sqlite3")

    async def payment(amount: int, operation_id: str = "") -> dict[str, object]:
        return dict(
            actuator.commit("payment", {"amount": amount}, operation_id=operation_id)
        )

    kernel = Castor(tools=[payment], destructive=["payment"])

    async def agent(proxy) -> dict[str, object]:
        return await proxy.syscall("payment", {"amount": 100})

    checkpoint = await kernel.run(agent, budgets={"_default": 1.0})
    assert checkpoint.status == "SUSPENDED_FOR_HITL"
    if decision == "approve":
        await HITLHandler().approve(checkpoint, kernel.gate, kernel.capability_manager)
    else:
        HITLHandler().reject(checkpoint, "evaluation rejection")
    checkpoint = await kernel.run(agent, checkpoint=checkpoint)

    metrics = actuator.metrics(expected_effects=1 if decision == "approve" else 0)
    journal_statuses = tuple(
        record.response.get("status", "COMMITTED")
        if isinstance(record.response, dict)
        else "COMPLETED"
        for record in checkpoint.syscall_log
    )
    return SecondaryWorkloadResult(
        workload="S-HITL",
        decision=decision,
        checkpoint_status=checkpoint.status,
        committed_effects=metrics.committed_effects,
        dup_commits=metrics.dup_commits,
        journal_statuses=journal_statuses,
    )
