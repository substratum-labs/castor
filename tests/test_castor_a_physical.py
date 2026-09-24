"""Castor A physical channel sentinel against the compiled Rust castord."""

from __future__ import annotations

import unittest

from tests.test_cognitive_recovery_castord import Daemon, expect, kind


class RustAuthorityChannelContract(unittest.TestCase):
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
            daemon.prepare()  # GrantCapability on control.sock and legal Agent work.
            daemon.arm()
            delivery = daemon.acquire()  # Bound payload from actuator.sock.
            self.assertEqual(delivery["delivery_outcome"], "Delivered")
            daemon.actuator.arrive()
            expect(daemon.settle(daemon.certificate()), "Settled")
        finally:
            daemon.close()


if __name__ == "__main__":
    unittest.main()
