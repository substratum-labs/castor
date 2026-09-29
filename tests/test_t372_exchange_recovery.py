"""T-372 recovery and wire contracts, with no real provider requests."""

import json
import socket
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from trusted_slot.real_model import (
    MODEL,
    FileBridgeModel,
    HostExchange,
    model_request,
    native_response,
)

from tests import test_t372_bridge as fixtures


class RecoveryTests(unittest.TestCase):
    def test_budget_constructor_never_overwrites_prior_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            first = HostExchange(directory, 30)
            original = json.loads((Path(directory) / "budget.json").read_bytes())
            original["reservations"].append(
                {"status": "RESERVED", "interaction_id": "uncertain"}
            )
            (Path(directory) / "budget.json").write_text(json.dumps(original))
            saved = (Path(directory) / "budget.json").read_bytes()
            with self.assertRaises((ValueError, FileExistsError)):
                HostExchange(directory, 30)
            self.assertEqual((Path(directory) / "budget.json").read_bytes(), saved)
            first.close()

    def test_unfinished_or_wrong_model_reply_cannot_bind_observation(self):
        fixture = fixtures.BridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        good = {**fixture.good(), "done": True, "model": MODEL}
        for altered in ({**good, "done": False}, {**good, "model": "other"}):
            with self.assertRaises(ValueError):
                native_response(fixture.native, altered)

    def test_nontext_content_is_rejected_before_transport(self):
        fixture = fixtures.BridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        native = json.loads(json.dumps(fixture.native))
        native["request"]["messages"][1]["content"] = {"unexpected": "object"}
        # Recompute the canonical checksum so this tests shape, not digest rejection.
        import hashlib

        native["request_digest"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    native["request"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
        )
        with self.assertRaises(ValueError):
            model_request(native)

    def test_bridge_returns_a_framed_error_and_has_a_wait_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            sockpath = str(Path(directory) / "socket")
            model = FileBridgeModel(sockpath, directory, deadline_seconds=0.15)
            self.addCleanup(model.close)
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(sockpath)
                content = json.dumps({"interaction_id": "i"}).encode()
                client.sendall(struct.pack(">I", len(content)) + content)
                header = client.recv(4)
                self.assertEqual(len(header), 4)
                frame = client.recv(struct.unpack(">I", header)[0])
                self.assertIn("error", json.loads(frame))

    def test_started_exchange_is_reaped_at_the_total_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            exchange = HostExchange(directory, 0.15)
            exchange.serve = lambda: time.sleep(5)
            exchange.start()
            try:
                time.sleep(0.5)
                self.assertFalse(exchange.worker.is_alive())
            finally:
                exchange.close()


if __name__ == "__main__":
    unittest.main()
