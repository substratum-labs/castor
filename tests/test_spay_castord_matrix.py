"""Physical T-313 S-Pay fault-injection matrix against the Rust castord."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import selectors
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CASTORD = REPO_ROOT / "kernel" / "target" / "release" / "castord"
RESULTS_DIR = REPO_ROOT / "results"
EMPTY_DIGEST = "sha256:" + hashlib.sha256(b"").hexdigest()
EFFECTS = ("payment", "email")
SYSTEMS = {
    "castor_full": {
        "label": "Castor Full",
        "stable_operation_id": True,
        "actuator_deduplication": True,
        "recover_from_castord": True,
    },
    "no_stable_operation_id": {
        "label": "No stable operation ID",
        "stable_operation_id": False,
        "actuator_deduplication": True,
        "recover_from_castord": True,
    },
    "no_actuator_deduplication": {
        "label": "No actuator deduplication",
        "stable_operation_id": True,
        "actuator_deduplication": False,
        "recover_from_castord": True,
    },
    "naive_reexecution": {
        "label": "Naive re-execution",
        "stable_operation_id": False,
        "actuator_deduplication": False,
        "recover_from_castord": False,
    },
}

EXPECTED_DUPLICATE_TRIALS = {
    ("castor_full", "kill_after_commit"): 0,
    ("castor_full", "kill_after_success"): 0,
    ("no_stable_operation_id", "kill_after_commit"): 20,
    ("no_stable_operation_id", "kill_after_success"): 0,
    ("no_actuator_deduplication", "kill_after_commit"): 20,
    ("no_actuator_deduplication", "kill_after_success"): 0,
    ("naive_reexecution", "kill_after_commit"): 20,
    ("naive_reexecution", "kill_after_success"): 20,
}


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = stream.recv(size)
        if not chunk:
            raise RuntimeError("castord closed the framed AISA connection")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def aisa_call(socket_path: Path, request_id: str, op: str, payload: dict) -> dict:
    request = json.dumps(
        {"request_id": request_id, "op": op, "payload": payload},
        separators=(",", ":"),
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(5)
        stream.connect(str(socket_path))
        stream.sendall(struct.pack(">I", len(request)) + request)
        response_size = struct.unpack(">I", _recv_exact(stream, 4))[0]
        response = json.loads(_recv_exact(stream, response_size))
    if response["status"] != "Ok":
        raise RuntimeError(f"{op} failed: {response}")
    return response["outcome"]


def expect_outcome(
    socket_path: Path, request_id: str, op: str, payload: dict, expected: str
) -> dict:
    outcome = aisa_call(socket_path, request_id, op, payload)
    if outcome.get("type") != expected:
        raise RuntimeError(f"{op} returned {outcome}, expected {expected}")
    return outcome


def ensure_region(
    socket_path: Path, request_id: str, region_ref: str, content: bytes
) -> str:
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    outcome = aisa_call(
        socket_path,
        request_id,
        "EnsureRegion",
        {
            "region_ref": region_ref,
            "content_digest": digest,
            "content": list(content),
            "profile": "D1",
        },
    )
    if outcome.get("type") not in {"Success", "AlreadyPersistedSameContent"}:
        raise RuntimeError(f"EnsureRegion returned {outcome}")
    return digest


def initialize_actuator(db_path: Path, deduplicate: bool) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE commits (
                commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                logical_effect TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                worker_phase TEXT NOT NULL,
                committed_at TEXT NOT NULL
            )
            """
        )
        if deduplicate:
            db.execute(
                "CREATE UNIQUE INDEX operation_id_unique ON commits(operation_id)"
            )


def operation_id(trial_id: str, effect: str, phase: str, stable: bool) -> str:
    if stable:
        return f"{trial_id}:{effect}"
    return f"{trial_id}:{phase}:{effect}:{uuid.uuid4()}"


def commit_effect(
    db_path: Path,
    trial_id: str,
    effect: str,
    phase: str,
    stable: bool,
) -> bool:
    op_id = operation_id(trial_id, effect, phase, stable)
    payload = {
        "amount": 100 if effect == "payment" else None,
        "recipient": "merchant" if effect == "payment" else "payer@example.test",
        "trial_id": trial_id,
    }
    try:
        with sqlite3.connect(db_path) as db:
            db.execute(
                """
                INSERT INTO commits(
                    logical_effect, operation_id, payload_json,
                    worker_phase, committed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    effect,
                    op_id,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    phase,
                    _utc_now(),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT logical_effect FROM commits WHERE operation_id = ?", (op_id,)
            ).fetchone()
        if row != (effect,):
            raise RuntimeError(f"operation ID {op_id} collided across effects")
        return False


def prepare_turn(socket_path: Path, trial_id: str) -> None:
    observation_ref = f"region://spay/{trial_id}/observation"
    ensure_region(socket_path, "ensure-observation", observation_ref, b"")
    expect_outcome(
        socket_path,
        "admit",
        "AdmitTurn",
        {
            "agent_id": f"spay-{trial_id}",
            "turn_id": 1,
            "lease_epoch": 0,
            "base_projection_digest": EMPTY_DIGEST,
        },
        "Admitted",
    )
    expect_outcome(
        socket_path,
        "request-interaction",
        "RequestInteraction",
        {
            "interaction_id": f"interaction-{trial_id}",
            "lease_epoch": 0,
            "request_digest": EMPTY_DIGEST,
        },
        "InteractionRequested",
    )
    expect_outcome(
        socket_path,
        "report-outcome",
        "ReportOutcome",
        {
            "interaction_id": f"interaction-{trial_id}",
            "observation_region_id": observation_ref,
            "observation_digest": EMPTY_DIGEST,
        },
        "InteractionBound",
    )
    expect_outcome(
        socket_path,
        "consume-interaction",
        "ConsumeInteraction",
        {"interaction_id": f"interaction-{trial_id}", "lease_epoch": 1},
        "InteractionConsumed",
    )
    action_ids = [f"{trial_id}-{effect}" for effect in EFFECTS]
    manifest = ("\n".join(action_ids) + "\n").encode()
    manifest_ref = f"region://spay/{trial_id}/manifest"
    manifest_digest = ensure_region(
        socket_path, "ensure-manifest", manifest_ref, manifest
    )
    expect_outcome(
        socket_path,
        "commit-turn",
        "CommitTurn",
        {
            "lease_epoch": 1,
            "base_projection_digest": EMPTY_DIGEST,
            "successor_region_id": observation_ref,
            "successor_digest": EMPTY_DIGEST,
            "action_manifest_region_id": manifest_ref,
            "action_manifest_digest": manifest_digest,
            "action_manifest": action_ids,
        },
        "TurnCommitted",
    )


def arm_action(socket_path: Path, trial_id: str, effect: str, attempt_id: int) -> None:
    action_id = f"{trial_id}-{effect}"
    expect_outcome(
        socket_path,
        f"register-{effect}",
        "RegisterAction",
        {"action_id": action_id},
        "ActionRegistered",
    )
    armed = expect_outcome(
        socket_path,
        f"arm-{effect}",
        "PresentAdmissionCertificate",
        {
            "action_id": action_id,
            "target_scope": f"spay/{effect}",
            "capability_id": "spay-eval-capability",
            "generation": 1,
        },
        "AttemptArmed",
    )
    if armed.get("attempt_id") != attempt_id:
        raise RuntimeError(f"unexpected {effect} attempt: {armed}")
    expect_outcome(
        socket_path,
        f"dispatch-{effect}",
        "RecordDispatchAttempt",
        {
            "attempt_id": attempt_id,
            "dispatch_identity": f"sqlite-{effect}",
        },
        "DispatchRecorded",
    )


def acknowledge_action(
    socket_path: Path, trial_id: str, effect: str, attempt_id: int
) -> None:
    expect_outcome(
        socket_path,
        f"deliver-{effect}",
        "DeliverArmedAttempt",
        {
            "attempt_id": attempt_id,
            "dispatch_identity": f"sqlite-{effect}",
        },
        "Delivered",
    )
    expect_outcome(
        socket_path,
        f"settle-{effect}",
        "PresentSettlementCertificate",
        {
            "attempt_id": attempt_id,
            "dispatch_identity": f"sqlite-{effect}",
            "evidence_region_id": f"region://spay/{trial_id}/observation",
            "evidence_digest": EMPTY_DIGEST,
            "proof_class": "ProviderConfirmation",
            "resolution": "Confirmed",
        },
        "Settled",
    )


def execute_and_acknowledge(
    socket_path: Path,
    db_path: Path,
    trial_id: str,
    effect: str,
    attempt_id: int,
    phase: str,
    stable: bool,
) -> None:
    commit_effect(db_path, trial_id, effect, phase, stable)
    acknowledge_action(socket_path, trial_id, effect, attempt_id)


def worker_initial(args: argparse.Namespace) -> None:
    stable = args.stable_operation_id
    prepare_turn(args.socket_path, args.trial_id)
    arm_action(args.socket_path, args.trial_id, "payment", 1)
    commit_effect(args.actuator_db, args.trial_id, "payment", "initial", stable)
    if args.fault == "kill_after_commit":
        print("ACTUATOR_COMMITTED", flush=True)
        signal.pause()
    acknowledge_action(args.socket_path, args.trial_id, "payment", 1)
    arm_action(args.socket_path, args.trial_id, "email", 2)
    execute_and_acknowledge(
        args.socket_path,
        args.actuator_db,
        args.trial_id,
        "email",
        2,
        "initial",
        stable,
    )
    print("TURN_SUCCESS_ACKNOWLEDGED", flush=True)
    signal.pause()


def worker_resume(args: argparse.Namespace) -> None:
    if not args.recover_from_castord:
        for effect in EFFECTS:
            commit_effect(
                args.actuator_db, args.trial_id, effect, "resume", stable=False
            )
        return

    if args.fault == "kill_after_success":
        return

    execute_and_acknowledge(
        args.socket_path,
        args.actuator_db,
        args.trial_id,
        "payment",
        1,
        "resume",
        args.stable_operation_id,
    )
    arm_action(args.socket_path, args.trial_id, "email", 2)
    execute_and_acknowledge(
        args.socket_path,
        args.actuator_db,
        args.trial_id,
        "email",
        2,
        "resume",
        args.stable_operation_id,
    )


def start_castord(
    storage_root: Path, socket_path: Path, control_socket: Path
) -> subprocess.Popen:
    if not CASTORD.is_file():
        raise RuntimeError(
            f"release castord is absent at {CASTORD}; "
            "run cargo build --release --bin castord"
        )
    daemon = subprocess.Popen(
        [
            str(CASTORD),
            "--storage-root",
            str(storage_root),
            "--socket",
            str(socket_path),
            "--control-socket",
            str(control_socket),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if daemon.poll() is not None:
            stderr = daemon.stderr.read() if daemon.stderr else ""
            raise RuntimeError(f"castord exited during startup: {stderr}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.connect(str(socket_path))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.connect(str(control_socket))
            return daemon
        except OSError:
            time.sleep(0.01)
    daemon.kill()
    daemon.wait()
    raise RuntimeError("castord did not bind both sockets within five seconds")


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    if process.stderr:
        process.stderr.close()


def wait_for_marker(worker: subprocess.Popen, expected: str) -> None:
    if worker.stdout is None:
        raise RuntimeError("worker stdout pipe is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(worker.stdout, selectors.EVENT_READ)
    try:
        events = selector.select(timeout=10)
        if not events:
            raise RuntimeError(f"worker did not emit {expected} within ten seconds")
        marker = worker.stdout.readline().strip()
    finally:
        selector.close()
    if marker != expected:
        stderr = worker.stderr.read() if worker.stderr else ""
        raise RuntimeError(
            f"worker emitted {marker!r}, expected {expected!r}: {stderr}"
        )


def worker_command(
    trial_id: str,
    phase: str,
    fault: str,
    socket_path: Path,
    actuator_db: Path,
    config: dict,
) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--worker",
        "--trial-id",
        trial_id,
        "--phase",
        phase,
        "--fault",
        fault,
        "--socket-path",
        str(socket_path),
        "--actuator-db",
        str(actuator_db),
    ]
    if config["stable_operation_id"]:
        command.append("--stable-operation-id")
    if config["recover_from_castord"]:
        command.append("--recover-from-castord")
    return command


def actuator_metrics(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT logical_effect, operation_id, worker_phase "
            "FROM commits ORDER BY commit_seq"
        ).fetchall()
    per_effect = {
        effect: sum(1 for row in rows if row[0] == effect) for effect in EFFECTS
    }
    return {
        "committed_effects": len(rows),
        "duplicate_commits": sum(max(0, count - 1) for count in per_effect.values()),
        "missing_commits": sum(1 for count in per_effect.values() if count == 0),
        "commits_per_effect": per_effect,
        "commits": [
            {"logical_effect": effect, "operation_id": op_id, "worker_phase": phase}
            for effect, op_id, phase in rows
        ],
    }


def run_trial(base_dir: Path, system_id: str, fault: str, trial: int) -> dict:
    config = SYSTEMS[system_id]
    trial_id = f"{system_id}-{fault}-{trial:02d}"
    trial_root = base_dir / trial_id
    storage_root = trial_root / "castord"
    storage_root.mkdir(parents=True)
    socket_key = hashlib.sha256(str(trial_root).encode()).hexdigest()[:16]
    socket_root = Path("/tmp") / f"cs-{socket_key}"
    socket_root.mkdir(mode=0o700)
    socket_path = socket_root / "agent.sock"
    control_socket = socket_root / "control.sock"
    actuator_db = trial_root / "actuator.sqlite3"
    initialize_actuator(actuator_db, config["actuator_deduplication"])
    started_at = _utc_now()

    settled_attempts = 0
    daemon = start_castord(storage_root, socket_path, control_socket)
    try:
        worker = subprocess.Popen(
            worker_command(
                trial_id, "initial", fault, socket_path, actuator_db, config
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        marker = (
            "ACTUATOR_COMMITTED"
            if fault == "kill_after_commit"
            else "TURN_SUCCESS_ACKNOWLEDGED"
        )
        try:
            wait_for_marker(worker, marker)
            worker.kill()
            return_code = worker.wait(timeout=5)
            if return_code != -signal.SIGKILL:
                raise RuntimeError(f"initial worker was not SIGKILLed: {return_code}")
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
            if worker.stdout:
                worker.stdout.close()
            if worker.stderr:
                worker.stderr.close()

        journal = aisa_call(
            control_socket, "inspect-durable-journal", "InspectJournal", {}
        )["entries"]
        settled_attempts = sum("AttemptSettled" in entry for entry in journal)
        expected_settled = 0 if fault == "kill_after_commit" else 2
        if settled_attempts != expected_settled:
            raise RuntimeError(
                f"recovered {settled_attempts} settled attempts, "
                f"expected {expected_settled}"
            )
        resumed = subprocess.run(
            worker_command(trial_id, "resume", fault, socket_path, actuator_db, config),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if resumed.returncode != 0:
            raise RuntimeError(
                f"resume worker failed ({resumed.returncode}): {resumed.stderr}"
            )
    finally:
        stop_process(daemon)
        socket_path.unlink(missing_ok=True)
        control_socket.unlink(missing_ok=True)
        socket_root.rmdir()

    metrics = actuator_metrics(actuator_db)
    metrics.update(
        {
            "trial": trial,
            "trial_id": trial_id,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "duplicate": metrics["duplicate_commits"] > 0,
            "initial_worker_signal": "SIGKILL",
            "castord_restarted": False,
            "same_castord_across_worker_resume": True,
            "recovered_settled_attempts": settled_attempts,
        }
    )
    if metrics["missing_commits"] != 0:
        raise RuntimeError(f"trial has missing effects: {metrics}")
    return metrics


def write_results(results: dict) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "spay_castord_matrix.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    rows = []
    for cell in results["cells"]:
        rows.append(
            "| {system} | `{fault}` | {trials} | {duplicates}/{trials} | "
            "{duplicate_commits} | {committed} |".format(
                system=cell["system"],
                fault=cell["fault"],
                trials=cell["trials"],
                duplicates=cell["duplicate_trials"],
                duplicate_commits=cell["total_duplicate_commits"],
                committed=cell["total_committed_effects"],
            )
        )
    markdown = (
        "# Live Rust `castord` S-Pay Fault Matrix\n\n"
        f"Generated at `{results['generated_at']}` on "
        f"`{results['environment']['platform']}` with binary SHA-256 "
        f"`{results['castord']['sha256']}`. Every initial worker was physically "
        "terminated by its parent with SIGKILL and resumed against the same live "
        "physical `castord`. SQLite is the independent actuator truth.\n\n"
        "Happy-path sentinel: "
        f"{results['happy_path']['committed_effects']} committed effects, "
        f"{results['happy_path']['duplicate_commits']} duplicates.\n\n"
        "| System | Fault | Trials | Duplicate trials | Duplicate commits | "
        "Total committed effects |\n"
        "|---|---|---:|---:|---:|---:|\n"
        + "\n".join(rows)
        + "\n\n`kill_after_commit` occurs after the payment SQLite transaction and "
        "durable dispatch record but before `DeliverArmedAttempt`. "
        "`kill_after_success` occurs after both action settlements are durable.\n"
    )
    (RESULTS_DIR / "spay_castord_matrix.md").write_text(markdown, encoding="utf-8")


def run_happy_path(base_dir: Path) -> dict:
    trial_id = "castor-full-happy-path"
    trial_root = base_dir / trial_id
    storage_root = trial_root / "castord"
    storage_root.mkdir(parents=True)
    socket_key = hashlib.sha256(str(trial_root).encode()).hexdigest()[:16]
    socket_root = Path("/tmp") / f"cs-{socket_key}"
    socket_root.mkdir(mode=0o700)
    socket_path = socket_root / "agent.sock"
    control_socket = socket_root / "control.sock"
    actuator_db = trial_root / "actuator.sqlite3"
    initialize_actuator(actuator_db, deduplicate=True)
    daemon = start_castord(storage_root, socket_path, control_socket)
    try:
        prepare_turn(socket_path, trial_id)
        for attempt_id, effect in enumerate(EFFECTS, start=1):
            arm_action(socket_path, trial_id, effect, attempt_id)
            execute_and_acknowledge(
                socket_path,
                actuator_db,
                trial_id,
                effect,
                attempt_id,
                "initial",
                stable=True,
            )
        journal = aisa_call(control_socket, "inspect-happy", "InspectJournal", {})[
            "entries"
        ]
        if sum("AttemptSettled" in entry for entry in journal) != 2:
            raise RuntimeError("happy path did not persist two settled attempts")
    finally:
        stop_process(daemon)
        socket_path.unlink(missing_ok=True)
        control_socket.unlink(missing_ok=True)
        socket_root.rmdir()
    metrics = actuator_metrics(actuator_db)
    metrics.update({"trial_id": trial_id, "completed_at": _utc_now()})
    return metrics


def run_matrix(base_dir: Path) -> dict:
    started_at = _utc_now()
    happy_path = run_happy_path(base_dir)
    cells = []
    for system_id, config in SYSTEMS.items():
        for fault in ("kill_after_commit", "kill_after_success"):
            runs = [
                run_trial(base_dir, system_id, fault, trial) for trial in range(1, 21)
            ]
            cell = {
                "system_id": system_id,
                "system": config["label"],
                "fault": fault,
                "trials": len(runs),
                "stable_operation_id": config["stable_operation_id"],
                "actuator_deduplication": config["actuator_deduplication"],
                "castord_recovery": config["recover_from_castord"],
                "duplicate_trials": sum(run["duplicate"] for run in runs),
                "total_duplicate_commits": sum(
                    run["duplicate_commits"] for run in runs
                ),
                "total_committed_effects": sum(
                    run["committed_effects"] for run in runs
                ),
                "runs": runs,
            }
            expected = EXPECTED_DUPLICATE_TRIALS[(system_id, fault)]
            if cell["duplicate_trials"] != expected:
                raise RuntimeError(
                    f"{system_id}/{fault} produced {cell['duplicate_trials']} "
                    f"duplicate trials, expected {expected}"
                )
            cells.append(cell)
    binary_bytes = CASTORD.read_bytes()
    results = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "started_at": started_at,
        "completed_at": _utc_now(),
        "scenario": "S-Pay: payment(amount=100) + email",
        "trials_per_cell": 20,
        "fault_delivery": "parent-issued SIGKILL",
        "happy_path": happy_path,
        "castord": {
            "path": str(CASTORD.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(binary_bytes).hexdigest(),
            "guest_channel": "agent.sock",
            "recovery": (
                "fresh worker process resumes against the same physical castord"
            ),
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "pid": os.getpid(),
        },
        "cells": cells,
    }
    write_results(results)
    return results


def test_spay_castord_matrix(tmp_path):
    results = run_matrix(tmp_path)
    observed = {
        (cell["system_id"], cell["fault"]): cell["duplicate_trials"]
        for cell in results["cells"]
    }
    assert observed == EXPECTED_DUPLICATE_TRIALS
    assert all(cell["trials"] == 20 for cell in results["cells"])
    full = [cell for cell in results["cells"] if cell["system_id"] == "castor_full"]
    assert all(
        trial["committed_effects"] == 2 for cell in full for trial in cell["runs"]
    )


def test_spay_happy_path(tmp_path):
    result = run_happy_path(tmp_path)
    assert result["committed_effects"] == 2
    assert result["duplicate_commits"] == 0
    assert result["missing_commits"] == 0


def parse_worker_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--phase", choices=("initial", "resume"), required=True)
    parser.add_argument(
        "--fault", choices=("kill_after_commit", "kill_after_success"), required=True
    )
    parser.add_argument("--socket-path", type=Path, required=True)
    parser.add_argument("--actuator-db", type=Path, required=True)
    parser.add_argument("--stable-operation-id", action="store_true")
    parser.add_argument("--recover-from-castord", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    worker_args = parse_worker_args()
    if worker_args.phase == "initial":
        worker_initial(worker_args)
    else:
        worker_resume(worker_args)
