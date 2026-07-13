"""S-Pay subprocess workers for Paper A systems and ablations.

Systems
-------
* ``c_full`` — Castor journal + kernel ``operation_id`` + actuator UNIQUE
* ``c_no_op_id`` — Castor journal, but tool ignores kernel id (random each call)
* ``c_no_dedup`` — Castor journal + stable id, actuator allows duplicate rows
* ``b_naive`` — no Castor; fresh random op ids; "resume" re-runs from scratch

Faults
------
* ``kill_after_commit`` — hang *inside* payment after world commit (DIE_AFTER_COMMIT)
* ``kill_after_success`` — hang in agent *after* payment syscall returns (journaled)
"""

from __future__ import annotations

import argparse
import asyncio
import threading
import uuid
from pathlib import Path
from typing import Literal

from castor import Castor
from castor.evals.actuator_bench import ActuatorBench
from castor.models.checkpoint import AgentCheckpoint
from castor.scheduler.persistence import CheckpointNotFoundError, CheckpointStore
from castor.scheduler.runner import AgentRunner

SystemName = Literal["c_full", "c_no_op_id", "c_no_dedup", "b_naive"]
FaultName = Literal["kill_after_commit", "kill_after_success"]
PhaseName = Literal["initial", "resume"]

COMMIT_MARKER = "ACTUATOR_COMMITTED"
PAYMENT_AMOUNT = 100
EMAIL_TO = "receipt@example.com"
EMAIL_BODY = "payment-ok"


def _checkpoint_url(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


def _make_payment_tool(
    actuator: ActuatorBench,
    *,
    system: SystemName,
    fault: FaultName,
    phase: PhaseName,
):
    def payment(amount: int, operation_id: str = "") -> dict[str, str]:
        if system == "c_no_op_id" or system == "b_naive":
            # Ablation / baseline: external effect identity is not kernel-stable.
            operation_id = uuid.uuid4().hex
        elif not operation_id:
            operation_id = uuid.uuid4().hex

        result = actuator.commit(
            "payment",
            {"amount": amount, "payee": "merchant"},
            operation_id=operation_id,
        )
        if phase == "initial" and fault == "kill_after_commit":
            print(COMMIT_MARKER, flush=True)
            threading.Event().wait()
        return result

    return payment


def _make_email_tool(actuator: ActuatorBench, *, system: SystemName):
    def send_email(to: str, body: str, operation_id: str = "") -> dict[str, str]:
        if system == "c_no_op_id" or system == "b_naive":
            operation_id = uuid.uuid4().hex
        elif not operation_id:
            operation_id = uuid.uuid4().hex
        return actuator.commit(
            "email",
            {"to": to, "body": body},
            operation_id=operation_id,
        )

    return send_email


def _make_balance_tool():
    def get_balance(account: str, operation_id: str = "") -> dict[str, object]:
        del operation_id
        return {"account": account, "balance": 10_000}

    return get_balance


def _make_llm_tool():
    """Deterministic mock LLM (stochastic coprocessor stand-in)."""

    def llm_decide(prompt: str, operation_id: str = "") -> dict[str, object]:
        del operation_id
        return {"decision": "pay", "amount": PAYMENT_AMOUNT, "prompt_len": len(prompt)}

    return llm_decide


async def _s_pay_agent(
    proxy, *, fault: FaultName, phase: PhaseName
) -> dict[str, object]:
    balance = await proxy.syscall("get_balance", {"account": "checking"})
    decision = await proxy.syscall(
        "llm_decide",
        {"prompt": f"balance={balance['balance']} payee=merchant"},
    )
    amount = int(decision["amount"])
    payment = await proxy.syscall("payment", {"amount": amount})
    if phase == "initial" and fault == "kill_after_success":
        print(COMMIT_MARKER, flush=True)
        threading.Event().wait()
    email = await proxy.syscall("send_email", {"to": EMAIL_TO, "body": EMAIL_BODY})
    return {
        "balance": balance,
        "decision": decision,
        "payment": payment,
        "email": email,
    }


def run_castor_worker(
    *,
    checkpoint_db: Path,
    actuator_db: Path,
    pid: str,
    phase: PhaseName,
    system: SystemName,
    fault: FaultName,
) -> AgentCheckpoint:
    """Execute one Castor-backed S-Pay attempt in this process."""
    assert system != "b_naive"
    store = CheckpointStore(_checkpoint_url(checkpoint_db))
    actuator = ActuatorBench(actuator_db, dedupe=(system != "c_no_dedup"))

    payment = _make_payment_tool(actuator, system=system, fault=fault, phase=phase)
    send_email = _make_email_tool(actuator, system=system)
    get_balance = _make_balance_tool()
    llm_decide = _make_llm_tool()

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

    kernel = Castor(
        tools=[get_balance, llm_decide, payment, send_email],
        runner_factory=runner_factory,
    )

    async def agent(proxy):
        return await _s_pay_agent(proxy, fault=fault, phase=phase)

    try:
        checkpoint = store.load(pid)
    except CheckpointNotFoundError:
        if phase == "resume":
            raise
        checkpoint = None

    return asyncio.run(
        kernel.run(
            agent,
            checkpoint=checkpoint,
            pid=pid,
            budgets={"_default": 0.0},
        )
    )


def run_naive_worker(
    *,
    actuator_db: Path,
    phase: PhaseName,
    fault: FaultName,
) -> None:
    """No-kernel baseline: re-executes the full effect path on every process."""
    actuator = ActuatorBench(actuator_db, dedupe=False)
    payment = _make_payment_tool(actuator, system="b_naive", fault=fault, phase=phase)
    send_email = _make_email_tool(actuator, system="b_naive")
    get_balance = _make_balance_tool()
    llm_decide = _make_llm_tool()

    balance = get_balance("checking")
    decision = llm_decide(f"balance={balance['balance']} payee=merchant")
    payment(int(decision["amount"]))
    if phase == "initial" and fault == "kill_after_success":
        # kill_after_commit already hung inside payment().
        print(COMMIT_MARKER, flush=True)
        threading.Event().wait()
    send_email(EMAIL_TO, EMAIL_BODY)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper A S-Pay worker")
    parser.add_argument("--checkpoint-db", type=Path, default=None)
    parser.add_argument("--actuator-db", type=Path, required=True)
    parser.add_argument("--pid", default="paper-a-s-pay")
    parser.add_argument("--phase", choices=("initial", "resume"), required=True)
    parser.add_argument(
        "--system",
        choices=("c_full", "c_no_op_id", "c_no_dedup", "b_naive"),
        required=True,
    )
    parser.add_argument(
        "--fault",
        choices=("kill_after_commit", "kill_after_success"),
        required=True,
    )
    args = parser.parse_args()

    if args.system == "b_naive":
        run_naive_worker(
            actuator_db=args.actuator_db,
            phase=args.phase,
            fault=args.fault,
        )
        return

    if args.checkpoint_db is None:
        raise SystemExit("--checkpoint-db is required for Castor systems")

    checkpoint = run_castor_worker(
        checkpoint_db=args.checkpoint_db,
        actuator_db=args.actuator_db,
        pid=args.pid,
        phase=args.phase,
        system=args.system,
        fault=args.fault,
    )
    if args.phase == "resume" and checkpoint.status != "COMPLETED":
        raise RuntimeError(
            f"resumed worker did not complete: status {checkpoint.status}"
        )


if __name__ == "__main__":
    main()
