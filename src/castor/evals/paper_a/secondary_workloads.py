"""Secondary Paper A workloads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from castor import Castor
from castor.evals.actuator_bench import ActuatorBench
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
