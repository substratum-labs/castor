"""Evaluation-local LangGraph S-Pay baseline worker.

The worker deliberately relies only on LangGraph's SQLite checkpointer.  Its
fresh actuator operation ids make the checkpoint boundary observable when a
process is killed after an external payment succeeds.
"""

from __future__ import annotations

import argparse
import threading
import uuid
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from castor.evals.actuator_bench import ActuatorBench
from castor.evals.paper_a.s_pay_worker import (
    COMMIT_MARKER,
    EMAIL_BODY,
    EMAIL_TO,
    PAYMENT_AMOUNT,
)

FaultName = Literal["kill_after_commit", "kill_after_success"]
PhaseName = Literal["initial", "resume"]


class SPayState(TypedDict, total=False):
    balance: dict[str, object]
    decision: dict[str, object]
    payment: dict[str, str]
    email: dict[str, str]


def build_graph(
    actuator: ActuatorBench, *, phase: PhaseName, fault: FaultName
) -> StateGraph:
    """Build the intentionally minimal checkpointed S-Pay graph."""

    def read_balance(_: SPayState) -> SPayState:
        return {"balance": {"account": "checking", "balance": 10_000}}

    def decide(_: SPayState) -> SPayState:
        return {
            "decision": {
                "decision": "pay",
                "amount": PAYMENT_AMOUNT,
                "prompt_len": 0,
            }
        }

    def pay(_: SPayState) -> SPayState:
        payment = actuator.commit(
            "payment",
            {"amount": PAYMENT_AMOUNT, "payee": "merchant"},
            operation_id=uuid.uuid4().hex,
        )
        if phase == "initial" and fault == "kill_after_commit":
            print(COMMIT_MARKER, flush=True)
            threading.Event().wait()
        return {"payment": payment}

    def suspend_after_success(_: SPayState) -> SPayState:
        if phase == "initial" and fault == "kill_after_success":
            print(COMMIT_MARKER, flush=True)
            threading.Event().wait()
        return {}

    def send_receipt(_: SPayState) -> SPayState:
        return {
            "email": actuator.commit(
                "email",
                {"to": EMAIL_TO, "body": EMAIL_BODY},
                operation_id=uuid.uuid4().hex,
            )
        }

    graph = StateGraph(SPayState)
    graph.add_node("balance", read_balance)
    graph.add_node("decision", decide)
    graph.add_node("payment", pay)
    graph.add_node("post_payment", suspend_after_success)
    graph.add_node("email", send_receipt)
    graph.add_edge(START, "balance")
    graph.add_edge("balance", "decision")
    graph.add_edge("decision", "payment")
    graph.add_edge("payment", "post_payment")
    graph.add_edge("post_payment", "email")
    graph.add_edge("email", END)
    return graph


def run_langgraph_worker(
    *,
    checkpoint_db: Path,
    actuator_db: Path,
    thread_id: str,
    phase: PhaseName,
    fault: FaultName,
) -> None:
    """Run one initial or resumed LangGraph S-Pay process."""

    actuator = ActuatorBench(actuator_db, dedupe=False)
    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
        app = build_graph(actuator, phase=phase, fault=fault).compile(
            checkpointer=saver
        )
        if phase == "initial":
            app.invoke({}, config=config)
        else:
            app.invoke(None, config=config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper A LangGraph S-Pay worker")
    parser.add_argument("--checkpoint-db", type=Path, required=True)
    parser.add_argument("--actuator-db", type=Path, required=True)
    parser.add_argument("--pid", default="paper-a-s-pay")
    parser.add_argument("--phase", choices=("initial", "resume"), required=True)
    parser.add_argument(
        "--fault",
        choices=("kill_after_commit", "kill_after_success"),
        required=True,
    )
    args = parser.parse_args()
    run_langgraph_worker(
        checkpoint_db=args.checkpoint_db,
        actuator_db=args.actuator_db,
        thread_id=args.pid,
        phase=args.phase,
        fault=args.fault,
    )


if __name__ == "__main__":
    main()
