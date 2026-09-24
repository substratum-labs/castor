"""Castor A physical channel sentinel against the compiled Rust castord."""

from __future__ import annotations

import unittest

from castor_client import AisaConnectionError, OperatorSession

from tests.dogfood.ring3_agent import AgentConfig, Ring3Agent
from tests.test_cognitive_recovery_castord import (
    AGENT,
    CAP,
    Daemon,
    digest,
    expect,
    kind,
)


class RustAuthorityChannelContract(unittest.TestCase):
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
            summary = OperatorSession(daemon.control).request("GetProjectionSummary", {})
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
