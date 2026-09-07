"""Carrier worker subprocess for physical fault-injection testing.

Performs Turn 1 admission, arming, and optional dispatch/actuator execution,
then notifies the parent process and pauses to await physical SIGKILL.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.recovery_actuator import RecoveryActuator


def recv_exact(stream: socket.socket, size: int) -> bytearray:
    result = bytearray()
    while len(result) < size:
        part = stream.recv(size - len(result))
        if not part:
            raise AssertionError("daemon closed stream prematurely")
        result.extend(part)
    return result


def send_ipc(sock_path: Path, op: str, payload: dict) -> dict:
    request = json.dumps(
        {"request_id": "worker", "op": op, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(5)
        stream.connect(str(sock_path))
        stream.sendall(struct.pack(">I", len(request)) + request)
        size = struct.unpack(">I", recv_exact(stream, 4))[0]
        return json.loads(recv_exact(stream, size))


def main():
    parser = argparse.ArgumentParser(description="Physical Carrier Worker")
    parser.add_argument("--agent-sock", type=Path, required=True)
    parser.add_argument("--actuator-db", type=Path, required=True)
    parser.add_argument(
        "--seam",
        choices=["arm", "t1_dispatch_committed", "t2_dispatch_late_arrival", "torn-settlement"],
        required=True,
    )
    parser.add_argument("--agent-id", default="recovery-agent")
    parser.add_argument("--turn", type=int, default=2)
    parser.add_argument("--base-digest", required=True)
    parser.add_argument("--op-id", default="recovery-payment-1")
    parser.add_argument("--scope", default="payment:fixture:merchant-42")
    parser.add_argument("--cap", default="recovery-cap")
    parser.add_argument("--generation", type=int, default=1)
    args = parser.parse_args()

    actuator = RecoveryActuator(args.actuator_db)

    # 1. Admit Turn
    admit_res = send_ipc(
        args.agent_sock,
        "AdmitTurn",
        {
            "agent_id": args.agent_id,
            "turn_id": args.turn,
            "lease_epoch": 0,
            "base_projection_digest": args.base_digest,
            "cap_id": args.cap,
        },
    )
    if admit_res.get("status") != "Ok":
        sys.stderr.write(f"Worker failed AdmitTurn: {admit_res}\n")
        sys.exit(1)

    # 2. Present Admission Certificate (Arm Attempt 1)
    arm_res = send_ipc(
        args.agent_sock,
        "PresentAdmissionCertificate",
        {
            "action_id": "a1",
            "target_scope": args.scope,
            "capability_id": args.cap,
            "generation": args.generation,
        },
    )
    if arm_res.get("status") != "Ok":
        sys.stderr.write(f"Worker failed PresentAdmissionCertificate: {arm_res}\n")
        sys.exit(1)

    # 3. Dispatch & Actuator execution based on seam
    if args.seam != "arm":
        dispatch_res = send_ipc(
            args.agent_sock,
            "RecordDispatchAttempt",
            {"attempt_id": 1, "dispatch_identity": args.op_id},
        )
        if dispatch_res.get("status") != "Ok":
            sys.stderr.write(f"Worker failed RecordDispatchAttempt: {dispatch_res}\n")
            sys.exit(1)

        if args.seam in ("t1_dispatch_committed", "torn-settlement"):
            # Physically execute on external actuator BEFORE crash
            actuator.arrive(args.op_id)

    # 4. Notify parent process of readiness for SIGKILL
    sys.stdout.write("READY_FOR_KILL\n")
    sys.stdout.flush()

    # 5. Wait for SIGKILL from parent
    time.sleep(60)


if __name__ == "__main__":
    main()
