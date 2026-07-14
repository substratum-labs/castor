"""Parent-process SIGKILL harness for Paper A S-Pay workers."""

from __future__ import annotations

import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.sqlite import SqliteSaver

from castor.evals.actuator_bench import ActuatorBench
from castor.evals.paper_a.s_pay_worker import COMMIT_MARKER
from castor.scheduler.persistence import CheckpointStore

SystemName = Literal[
    "c_full",
    "c_no_op_id",
    "c_no_dedup",
    "b_naive",
    "b_langgraph",
    "s_bypass",
]
FaultName = Literal["kill_after_commit", "kill_after_success"]

_WORKER_MODULE = "castor.evals.paper_a.s_pay_worker"
_LANGGRAPH_WORKER_MODULE = "castor.evals.paper_a.langgraph_worker"
_MARKER_TIMEOUT_SECONDS = 15
_PROCESS_TIMEOUT_SECONDS = 15
_LANGGRAPH_CHECKPOINT_POLL_SECONDS = 0.01

# S-Pay fully complete: one payment + one email (reads/LLM are not actuators).
EXPECTED_EFFECTS_COMPLETE = 2


@dataclass(frozen=True)
class SPayHarnessResult:
    """Observed outcome of one kill-and-resume S-Pay trial."""

    system: str
    fault: str
    committed_effects: int
    dup_commits: int
    missing_commits: int
    resume_success: bool
    resumed_checkpoint_status: str | None
    commits: tuple[str, ...]


def run_s_pay_kill_trial(
    work_dir: Path,
    *,
    system: SystemName,
    fault: FaultName,
    pid: str = "paper-a-s-pay",
) -> SPayHarnessResult:
    """Kill worker after payment commit marker, then resume / re-exec."""
    work_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_db = work_dir / "checkpoints.sqlite3"
    actuator_db = work_dir / "actuator.sqlite3"

    initial = _start_worker(
        checkpoint_db=checkpoint_db,
        actuator_db=actuator_db,
        pid=pid,
        phase="initial",
        system=system,
        fault=fault,
    )
    try:
        _wait_for_commit_marker(initial)
        if system == "b_langgraph" and fault == "kill_after_success":
            _wait_for_langgraph_payment_checkpoint(checkpoint_db, pid)
        initial.kill()
        return_code = initial.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
    finally:
        if initial.poll() is None:
            initial.kill()
            initial.wait(timeout=_PROCESS_TIMEOUT_SECONDS)

    if return_code != -signal.SIGKILL:
        raise RuntimeError(
            f"initial worker was not SIGKILLed: return code {return_code}"
        )

    resumed = _start_worker(
        checkpoint_db=checkpoint_db,
        actuator_db=actuator_db,
        pid=pid,
        phase="resume",
        system=system,
        fault=fault,
    )
    resume_stdout, resume_stderr = resumed.communicate(timeout=_PROCESS_TIMEOUT_SECONDS)
    resume_ok = resumed.returncode == 0

    resumed_status: str | None = None
    if system not in {"b_naive", "b_langgraph"}:
        if not resume_ok:
            raise RuntimeError(
                "resume worker failed: "
                f"return code {resumed.returncode}; stdout={resume_stdout!r}; "
                f"stderr={resume_stderr!r}"
            )
        resumed_status = (
            CheckpointStore(_checkpoint_url(checkpoint_db)).load(pid).status
        )
        if resumed_status != "COMPLETED":
            raise RuntimeError(
                f"resumed checkpoint was not completed: status {resumed_status}"
            )
        resume_ok = True
    else:
        # Baselines resume by re-exec; success means process exited 0.
        if not resume_ok:
            raise RuntimeError(
                "naive resume worker failed: "
                f"return code {resumed.returncode}; stdout={resume_stdout!r}; "
                f"stderr={resume_stderr!r}"
            )

    dedupe = system not in {"c_no_dedup", "b_naive", "b_langgraph"}
    metrics = ActuatorBench(actuator_db, dedupe=dedupe).metrics(
        expected_effects=EXPECTED_EFFECTS_COMPLETE
    )
    commits = tuple(
        row["effect_name"]
        for row in ActuatorBench(actuator_db, dedupe=dedupe).list_commits()
    )

    return SPayHarnessResult(
        system=system,
        fault=fault,
        committed_effects=metrics.committed_effects,
        dup_commits=metrics.dup_commits,
        missing_commits=metrics.missing_effects,
        resume_success=resume_ok,
        resumed_checkpoint_status=resumed_status,
        commits=commits,
    )


def _start_worker(
    *,
    checkpoint_db: Path,
    actuator_db: Path,
    pid: str,
    phase: str,
    system: SystemName,
    fault: FaultName,
) -> subprocess.Popen[str]:
    worker_module = (
        _LANGGRAPH_WORKER_MODULE if system == "b_langgraph" else _WORKER_MODULE
    )
    cmd = [
        sys.executable,
        "-m",
        worker_module,
        "--actuator-db",
        str(actuator_db),
        "--pid",
        pid,
        "--phase",
        phase,
        "--fault",
        fault,
    ]
    if system != "b_langgraph":
        cmd.extend(["--system", system])
    if system != "b_naive":
        cmd.extend(["--checkpoint-db", str(checkpoint_db)])
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _checkpoint_url(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


def _wait_for_langgraph_payment_checkpoint(checkpoint_db: Path, thread_id: str) -> None:
    """Wait until LangGraph durably schedules post-payment for ``thread_id``."""
    deadline = time.monotonic() + _MARKER_TIMEOUT_SECONDS
    last_channel_names: tuple[str, ...] = ()
    config = {"configurable": {"thread_id": thread_id}}

    while time.monotonic() < deadline:
        if checkpoint_db.exists():
            with SqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
                for checkpoint in saver.list(config):
                    channel_values = checkpoint.checkpoint.get("channel_values", {})
                    last_channel_names = tuple(sorted(channel_values))
                    if {
                        "payment",
                        "branch:to:post_payment",
                    }.issubset(channel_values):
                        return
        time.sleep(_LANGGRAPH_CHECKPOINT_POLL_SECONDS)

    raise RuntimeError(
        "LangGraph payment checkpoint was not durable before SIGKILL: "
        f"thread_id={thread_id!r}; checkpoint_db={checkpoint_db}; "
        f"last_channel_values={last_channel_names!r}"
    )


def _wait_for_commit_marker(process: subprocess.Popen[str]) -> None:
    if process.stdout is None:
        raise RuntimeError("initial worker stdout is unavailable")

    lines: queue.Queue[str | None] = queue.Queue()

    def collect_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=collect_stdout, daemon=True)
    reader.start()
    try:
        while True:
            line = lines.get(timeout=_MARKER_TIMEOUT_SECONDS)
            if line is None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                raise RuntimeError(
                    f"initial worker exited before {COMMIT_MARKER}: "
                    f"return code {process.wait()}; stderr={stderr!r}"
                )
            if line.rstrip("\n") == COMMIT_MARKER:
                return
    except queue.Empty as error:
        raise RuntimeError(
            f"initial worker did not emit {COMMIT_MARKER} in time"
        ) from error
