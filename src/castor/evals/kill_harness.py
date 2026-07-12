"""Hard-kill evaluation harness for the external ActuatorBench boundary."""

from __future__ import annotations

import queue
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from castor.evals.actuator_bench import ActuatorBench

_WORKER_MODULE = "castor.evals.worker"
_COMMIT_MARKER = "ACTUATOR_COMMITTED"
_MARKER_TIMEOUT_SECONDS = 10
_PROCESS_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class HarnessResult:
    """Observed outcome of one kill-and-resume experiment."""

    committed_effects: int
    dup_commits: int
    missing_commits: int
    resume_success: bool


def run_kill_after_commit(tmp_path: Path) -> HarnessResult:
    """Kill a worker after its actuator commit, then resume it in a new process."""
    checkpoint_db = tmp_path / "checkpoints.sqlite3"
    actuator_db = tmp_path / "actuator.sqlite3"
    pid = "kill-harness-payment"
    initial = _start_worker(checkpoint_db, actuator_db, pid, "initial")

    try:
        _wait_for_commit_marker(initial)
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

    resumed = _start_worker(checkpoint_db, actuator_db, pid, "resume")
    resume_stdout, resume_stderr = resumed.communicate(timeout=_PROCESS_TIMEOUT_SECONDS)
    if resumed.returncode != 0:
        raise RuntimeError(
            "resume worker failed: "
            f"return code {resumed.returncode}; stdout={resume_stdout!r}; "
            f"stderr={resume_stderr!r}"
        )

    metrics = ActuatorBench(actuator_db).metrics(expected_effects=1)
    return HarnessResult(
        committed_effects=metrics.committed_effects,
        dup_commits=metrics.dup_commits,
        missing_commits=metrics.missing_effects,
        resume_success=True,
    )


def _start_worker(
    checkpoint_db: Path, actuator_db: Path, pid: str, phase: str
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            _WORKER_MODULE,
            "--checkpoint-db",
            str(checkpoint_db),
            "--actuator-db",
            str(actuator_db),
            "--pid",
            pid,
            "--phase",
            phase,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_commit_marker(process: subprocess.Popen[str]) -> None:
    if process.stdout is None:
        raise RuntimeError("initial worker stdout is unavailable")

    lines: queue.Queue[str | None] = queue.Queue()

    def collect_stdout() -> None:
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
                    "initial worker exited before ACTUATOR_COMMITTED: "
                    f"return code {process.wait()}; stderr={stderr!r}"
                )
            if line.rstrip("\n") == _COMMIT_MARKER:
                return
    except queue.Empty as error:
        raise RuntimeError(
            "initial worker did not emit ACTUATOR_COMMITTED in time"
        ) from error
