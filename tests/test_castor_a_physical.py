"""Castor A physical channel sentinel against the compiled Rust castord."""

from __future__ import annotations

import unittest

from tests.test_cognitive_recovery_castord import Daemon, kind


class RustAuthorityChannelContract(unittest.TestCase):
    def test_agent_channel_rejects_privileged_operations_without_append(self) -> None:
        daemon = Daemon()
        try:
            before = daemon.journal()
            for op in (
                "GrantCapability",
                "PresentSettlementCertificate",
                "AcquireDispatch",
            ):
                with self.subTest(op=op):
                    self.assertEqual(kind(daemon.call(op, {})), "UnauthorizedOpcode")
                    self.assertEqual(daemon.journal(), before)
        finally:
            daemon.close()


if __name__ == "__main__":
    unittest.main()
