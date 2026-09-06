"""C1 socket identity and canonical registration regression tests."""
import json
import os
import stat
import unittest

from test_cognitive_recovery_castord import ADAPTER, AGENT, CAP, Daemon, encoded, expect


class EvidenceBoundary(unittest.TestCase):
    def fixture(self):
        daemon = Daemon()
        self.addCleanup(daemon.close)
        daemon.prepare()
        return daemon

    def test_peer_uid_and_channel_allowlist(self):
        d = self.fixture()
        self.assertEqual(stat.S_IMODE(d.evidence.stat().st_mode), 0o600)
        before = d.journal()
        for op in ("InspectJournal", "EnsureRegion", "GrantCapability", "Replay"):
            expect(d.call(op, {}, "evidence"), "UnauthorizedOpcode")
        self.assertEqual(d.journal(), before)
        config = json.loads(d.trust.read_bytes())
        config["peer_uid"] = os.getuid() + 1
        d.trust.write_bytes(encoded(config))
        d.restart()
        before = d.journal()
        # The actual connected UID is denied before opcode dispatch.
        expect(d.call("InspectJournal", {}, "evidence"), "RejectedBindingOrIssuer")
        self.assertEqual(d.journal(), before)

    def test_unknown_canonical_binding_rejected_without_append(self):
        d = self.fixture()
        config = json.loads(d.trust.read_bytes())
        del config["canonical_scopes"]["a3"]
        d.trust.write_bytes(encoded(config))
        d.restart()
        before = d.journal()
        expect(d.call("RegisterAction", {
            "action_id": "a3", "agent_id": AGENT, "action_family": ADAPTER,
            "cap_id": CAP, "target_scope": "payment:fixture:alias",
        }), "RejectedPrecondition")
        self.assertEqual(d.journal(), before)


if __name__ == "__main__":
    unittest.main()
