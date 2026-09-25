"""Castor A physical channel sentinel against the compiled Rust castord."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from castor_client import AisaConnectionError, AisaGatewayError, OperatorSession

from tests.dogfood.ring3_agent import AgentConfig, Ring3Agent
from tests.fixtures.castor_a_trace_support import (
    prepare_trace,
    report_model_after_request,
)
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
    def test_roche_mode_exposes_only_agent_socket_inode_to_guest_uid(self) -> None:
        daemon = Daemon(sandbox=True)
        try:
            self.assertEqual(stat.S_IMODE(daemon.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(daemon.agent.stat().st_mode), 0o666)
            for host_socket in (daemon.control, daemon.evidence, daemon.delivery):
                with self.subTest(socket=host_socket.name):
                    self.assertEqual(stat.S_IMODE(host_socket.stat().st_mode), 0o600)
        finally:
            daemon.close()

    def test_fresh_install_client_completes_governed_turn(self) -> None:
        root = Path(__file__).resolve().parents[1]
        wheel = (
            root / "packages/castor-client/dist/castor_client-0.7.0a1-py3-none-any.whl"
        )
        fixture = root / "tests/fixtures/castor_a_installed_agent.py"
        self.assertTrue(wheel.is_file(), "build the Castor A client wheel first")
        daemon = Daemon()
        try:
            config, observation = prepare_trace(daemon)
            with tempfile.TemporaryDirectory() as temporary:
                environment = Path(temporary) / "installed-client"
                create = subprocess.run(
                    [sys.executable, "-m", "venv", str(environment)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(create.returncode, 0, create.stderr)
                python = environment / "bin/python"
                install = subprocess.run(
                    [str(python), "-m", "pip", "install", "--no-index", str(wheel)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(install.returncode, 0, install.stderr)
                process = subprocess.Popen(
                    [str(python), "-I", str(fixture)],
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key != "PYTHONPATH"
                    }
                    | {"CASTOR_A_TRACE_CONFIG": json.dumps(config)},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    stdout, stderr = report_model_after_request(
                        daemon, process, str(config["interaction_id"]), observation
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                outcomes = json.loads(stdout)
                self.assertEqual(
                    outcomes,
                    [
                        "Admitted",
                        "InteractionRequested",
                        "InteractionConsumed",
                        "TurnCommitted",
                        "ActionRegistered",
                        "AttemptArmed",
                        "DispatchRecorded",
                    ],
                )

            delivery = daemon.acquire()
            self.assertEqual(delivery["delivery_outcome"], "Delivered")
            self.assertEqual(daemon.actuator.arrive(), "Committed")
            expect(daemon.settle(daemon.certificate()), "Settled")
            self.assertEqual(daemon.actuator.count(), 1)
            before_restart = daemon.journal()
            daemon.restart()
            self.assertEqual(daemon.journal(), before_restart)
            self.assertEqual(daemon.actuator.count(), 1)
        finally:
            daemon.close()

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
                    "import importlib.util, json, pathlib, sys; "
                    "from castor_client import AgentRequest, AgentSession; "
                    "assert importlib.util.find_spec('castor') is None; "
                    "assert not (pathlib.Path(sys.executable).parent / "
                    "'castor').exists(); "
                    "assert not (pathlib.Path(sys.executable).parent / "
                    "'castor-mcp').exists(); "
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

    def test_client_exposes_physical_agent_channel_denial_code(self) -> None:
        daemon = Daemon()
        try:
            before = daemon.journal()
            projection = daemon.summary()
            operator_on_guest_socket = OperatorSession(daemon.agent)
            with self.assertRaises(AisaGatewayError) as raised:
                operator_on_guest_socket.request("GrantCapability", {})
            self.assertEqual(raised.exception.code, "UnauthorizedOpcode")
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
