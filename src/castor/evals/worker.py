"""Subprocess worker for the ActuatorBench SIGKILL evaluation."""

from __future__ import annotations

import argparse
import asyncio
import threading
from pathlib import Path

from castor import Castor
from castor.evals.actuator_bench import ActuatorBench
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore
from castor.scheduler.runner import AgentRunner


def _checkpoint_url(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


def run_worker(
    *, checkpoint_db: Path, actuator_db: Path, pid: str, phase: str
) -> AgentCheckpoint:
    """Execute one payment attempt in a fresh process."""
    store = CheckpointStore(_checkpoint_url(checkpoint_db))
    actuator = ActuatorBench(actuator_db)

    def payment(amount: int, operation_id: str = "") -> dict[str, str]:
        result = actuator.commit(
            "payment", {"amount": amount}, operation_id=operation_id
        )
        if phase == "initial":
            print("ACTUATOR_COMMITTED", flush=True)
            threading.Event().wait()
        return result

    def runner_factory(gate, capability_manager, **kwargs):
        return AgentRunner(
            gate,
            capability_manager,
            lodge=kwargs.get("lodge"),
            agent_registry=kwargs.get("agent_registry"),
            checkpoint_store=store,
            structured_results=kwargs.get("structured_results", False),
            speculative=kwargs.get("speculative", False),
            scheduler=kwargs.get("scheduler"),
        )

    kernel = Castor(tools=[payment], runner_factory=runner_factory)

    async def payment_agent(proxy):
        return await proxy.syscall("payment", {"amount": 100})

    try:
        checkpoint = store.load(pid)
    except CheckpointNotFoundError:
        if phase == "resume":
            raise
        checkpoint = None

    return asyncio.run(
        kernel.run(
            payment_agent,
            checkpoint=checkpoint,
            pid=pid,
            budgets={"_default": 0.0},
        )
    )


def main() -> None:
    """Run one worker phase from the command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-db", type=Path, required=True)
    parser.add_argument("--actuator-db", type=Path, required=True)
    parser.add_argument("--pid", required=True)
    parser.add_argument("--phase", choices=("initial", "resume"), required=True)
    args = parser.parse_args()
    checkpoint = run_worker(
        checkpoint_db=args.checkpoint_db,
        actuator_db=args.actuator_db,
        pid=args.pid,
        phase=args.phase,
    )
    if args.phase == "resume" and checkpoint.status != "COMPLETED":
        raise RuntimeError(
            f"resumed worker did not complete: checkpoint status {checkpoint.status}"
        )


if __name__ == "__main__":
    main()
