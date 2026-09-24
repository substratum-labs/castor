"""Castor A physical channel sentinel against the compiled Rust castord."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from castor_client import AisaConnectionError, OperatorSession

from tests.dogfood.ring3_agent import AgentConfig, Ring3Agent
from tests.test_cognitive_recovery_castord import (
    AGENT,
    CAP,
    OP_ID,
    Daemon,
    digest,
    expect,
    kind,
)


class RustAuthorityChannelContract(unittest.TestCase):
    def test_built_client_wheel_reads_physical_projection(self) -> None:
        wheel = (
            Path(__file__).resolve().parents[1]
            / "packages/castor-client/dist/castor_client-0.7.0a1-py3-none-any.whl"
        )
        self.assertTrue(wheel.is_file(), "build the Castor A client wheel first")
        daemon = Daemon()
        try:
            source = (
                "import json, sys; "
                "sys.path.insert(0, sys.argv[1]); "
                "from castor_client import OperatorSession; "
                "print(json.dumps(OperatorSession(sys.argv[2]).request("
                "'GetProjectionSummary', {})['outcome']))"
            )
            probe = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    source,
                    str(wheel),
                    str(daemon.control),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(json.loads(probe.stdout), daemon.summary())
        finally:
            daemon.close()

    def test_fresh_install_client_drives_bound_action(self) -> None:
        wheel = (
            Path(__file__).resolve().parents[1]
            / "packages/castor-client/dist/castor_client-0.7.0a1-py3-none-any.whl"
        )
        self.assertTrue(wheel.is_file(), "build the Castor A client wheel first")
        daemon = Daemon()
        try:
            daemon.prepare()
            before = len(daemon.journal())
            with tempfile.TemporaryDirectory() as root:
                environment = Path(root) / "installed-client"
                create = subprocess.run(
                    [sys.executable, "-m", "venv", str(environment)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(create.returncode, 0, create.stderr)
                python = environment / "bin/python"
                install = subprocess.run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--no-index",
                        "--no-deps",
                        str(wheel),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(install.returncode, 0, install.stderr)
                source = (
                    "import json, sys; "
                    "from castor_client import AgentRequest, AgentSession; "
                    "agent = AgentSession(sys.argv[1]); "
                    "admission = json.loads(sys.argv[2]); "
                    "armed = agent.send(AgentRequest("
                    "'PresentAdmissionCertificate', admission)); "
                    "dispatched = agent.send(AgentRequest("
                    "'RecordDispatchAttempt', "
                    "{'attempt_id': 1, 'dispatch_identity': sys.argv[3]})); "
                    "print(json.dumps([armed.outcome_type, dispatched.outcome_type]))"
                )
                probe = subprocess.run(
                    [
                        str(python),
                        "-I",
                        "-c",
                        source,
                        str(daemon.agent),
                        json.dumps(daemon.admission()),
                        OP_ID,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertEqual(
                    json.loads(probe.stdout), ["AttemptArmed", "DispatchRecorded"]
                )
            self.assertGreater(len(daemon.journal()), before)
            delivery = daemon.acquire()
            self.assertEqual(delivery["delivery_outcome"], "Delivered")
            daemon.actuator.arrive()
            expect(daemon.settle(daemon.certificate()), "Settled")
            self.assertEqual(daemon.actuator.count(), 1)
        finally:
            daemon.close()

    @staticmethod
    def guest(daemon: Daemon, turn_id: int) -> Ring3Agent:
        return Ring3Agent(
            AgentConfig(
                socket_path=daemon.agent,
                agent_id=AGENT,
                turn_id=turn_id,
                lease_epoch=0,
                consume_lease_epoch=1,
                base_projection_digest=daemon.base,
                capability_id=CAP,
                generation=daemon.generation,
                interaction_id=f"castor-a-{turn_id}",
                request_digest=digest(b"castor-a-probe"),
            )
        )

    def test_agent_channel_rejects_privileged_operations_without_append(self) -> None:
        daemon = Daemon()
        try:
            before = daemon.journal()
            projection = daemon.summary()
            for op in (
                "GrantCapability",
                "PresentSettlementCertificate",
                "AcquireDispatch",
            ):
                with self.subTest(op=op):
                    self.assertEqual(kind(daemon.call(op, {})), "UnauthorizedOpcode")
                    self.assertEqual(daemon.journal(), before)
                    self.assertEqual(daemon.summary(), projection)
        finally:
            daemon.close()

    def test_control_actuator_and_evidence_channels_remain_usable(self) -> None:
        daemon = Daemon()
        try:
            operator = OperatorSession(daemon.control)
            summary = operator.request("GetProjectionSummary", {})
            self.assertIsInstance(summary, dict)
            daemon.prepare()  # GrantCapability on control.sock and legal Agent work.
            daemon.arm()
            delivery = daemon.acquire()  # Bound payload from actuator.sock.
            self.assertEqual(delivery["delivery_outcome"], "Delivered")
            daemon.actuator.arrive()
            expect(daemon.settle(daemon.certificate()), "Settled")
        finally:
            daemon.close()

    def test_python_guest_fails_closed_when_daemon_dies(self) -> None:
        daemon = Daemon()
        try:
            daemon.prepare()
            before = daemon.journal()
            daemon.kill()
            guest = self.guest(daemon, 2)
            with self.assertRaises(AisaConnectionError):
                guest._expect("AdmitTurn", daemon.admit_payload(2), "Admitted")
            daemon.start()
            self.assertEqual(daemon.journal(), before)
            self.assertEqual(daemon.actuator.count(), 0)
        finally:
            daemon.close()

    def test_python_guest_observes_unknown_after_arm_and_restart(self) -> None:
        daemon = Daemon()
        try:
            daemon.prepare()
            daemon.arm(dispatch=False)
            daemon.restart()
            daemon.base = daemon.durable_projection_digest()
            guest = self.guest(daemon, 2)
            admitted = guest._expect("AdmitTurn", daemon.admit_payload(2), "Admitted")
            attempts = admitted["unsettled_effects_snapshot"]["attempts"]
            self.assertEqual(attempts[0]["status"], "ArmedUnknown")
            self.assertEqual(daemon.actuator.count(), 0)
        finally:
            daemon.close()


if __name__ == "__main__":
    unittest.main()
