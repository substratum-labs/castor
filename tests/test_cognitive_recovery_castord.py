"""T-314-B physical RED contracts (RFC R1–R12, not TLA action-name tests).

Run through Cargo (selects the freshly built binary), or:
  export CASTORD_BINARY=kernel/target/debug/castord
  python3 tests/test_cognitive_recovery_castord.py -v

R11's two-attempt reconstruction fixture lives in the Rust companion. No
third-party Python packages, production sockets, test opcodes, xfail or skips.

Phase-C transport seams: the RFC freezes semantics but not JSON envelopes for
QueryOperation, EvidenceCertificate, or the Core-authored initial observation.
The helpers below use `descriptor: {type: QueryOperation, ...}`, bound receipt
fields on PresentSettlementCertificate, and `unsettled_effects_snapshot` in the
AdmitTurn result. `CASTORD_EVIDENCE_TRUST_CONFIG` provisions a test-only
HMAC receipt key, peer UID, canonical scopes, actuator store and budget. The
baseline ignores it. These are explicit harness conventions, not claimed frozen
RFC field layouts. Adapt those helpers to the eventual wire schema, preserving
all behavior assertions. A missing evidence.sock is a real boundary gap, never
silently routed to agent.sock or treated as successful authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("CASTORD_BINARY", ROOT / "kernel/target/debug/castord"))
SCOPE = "payment:fixture:merchant-42"
ADAPTER = "c04:generic"
OP_ID = "recovery-payment-1"
SIGNING_KEY = b"test-only-actuator-receipt-key-not-for-production"
CAP = "recovery-cap"
AGENT = "recovery-agent"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def recv_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        part = stream.recv(size - len(result))
        if not part:
            raise AssertionError("daemon closed an incomplete AISA response")
        result.extend(part)
    return result


def kind(response):
    return (
        response.get("error", {}).get("code")
        if response.get("status") == "Error"
        else response.get("outcome", {}).get("type")
    )


def expect(response, expected):
    assert kind(response) == expected, (
        f"expected {expected}, actual wire response: {response}"
    )
    return response.get("outcome", {})


class Actuator:
    """External SQLite endpoint, independent of castord's journal and process.

    The subprocess below represents arrival of a delayed network packet. A
    stable ID and atomic tombstone/commit transaction are actuator assumptions,
    NOT evidence of Core implementing duplicate suppression or authentication.
    """

    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE operations (op TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE commits (op TEXT PRIMARY KEY)")

    def query(self):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT state FROM operations WHERE op=?", (OP_ID,)
            ).fetchone()
        return row[0] if row else "not_found"

    def cancel(self):
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM operations WHERE op=?", (OP_ID,)
            ).fetchone()
            assert row is None or row[0] == "TerminatedRejected", (
                "cannot tombstone a committed operation"
            )
            db.execute(
                "INSERT OR REPLACE INTO operations VALUES (?, 'TerminatedRejected')",
                (OP_ID,),
            )
        return self.receipt("NotApplied")

    def arrive(self):
        code = """import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
 db.execute("BEGIN IMMEDIATE")
 row=db.execute("SELECT state FROM operations WHERE op=?",(sys.argv[2],)).fetchone()
 if row is None:
  db.execute("INSERT INTO operations VALUES (?, 'Committed')",(sys.argv[2],))
  db.execute("INSERT INTO commits VALUES (?)",(sys.argv[2],))
  print("Committed")
 else:
  print(row[0])
"""
        return subprocess.check_output(
            [sys.executable, "-c", code, str(self.path), OP_ID], text=True, timeout=5
        ).strip()

    def count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT count(*) FROM commits").fetchone()[0]

    def receipt(self, resolution):
        # Adapter-side authenticated fixture receipt, distinct from the Region
        # content hash. Trust provisioning is an explicit Phase-C harness seam.
        receipt = {
            "attempt_id": 1,
            "stable_operation_id": OP_ID,
            "adapter_id": ADAPTER,
            "issuer": "fixture-evidence-service",
            "request_digest": SCOPE,
            "settlement_schema_version": 1,
            "resolution": resolution,
            "actuator_state": self.query(),
        }
        receipt["signature"] = hmac.new(
            SIGNING_KEY, encoded(receipt), hashlib.sha256
        ).hexdigest()
        return receipt


class Daemon:
    def __init__(self):
        # Explicit /tmp avoids macOS's long per-user TMPDIR Unix socket limit.
        self.temp = tempfile.TemporaryDirectory(prefix="cr-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.agent = self.root / "agent.sock"
        self.control = self.root / "control.sock"
        self.evidence = self.root / "evidence.sock"
        self.log = self.root / "daemon.log"
        self.process = None
        self.turn = 1
        self.base = digest(b"")
        self.generation = 1
        self.actuator = Actuator(self.root / "actuator.sqlite")
        self.trust = self.root / "evidence-trust.json"
        self.trust.write_bytes(
            encoded(
                {
                    "issuer": "fixture-evidence-service",
                    "peer_uid": os.getuid(),
                    "adapter_id": ADAPTER,
                    "receipt_algorithm": "HMAC-SHA256",
                    "key_hex": SIGNING_KEY.hex(),
                    "actuator_db": str(self.actuator.path),
                    "probe_budget": 2,
                    "canonical_scopes": {
                        "a1": SCOPE,
                        "a2": SCOPE,
                        "a3": "payment:fixture:other",
                    },
                }
            )
        )
        self.trust.chmod(0o600)
        self.start()

    def start(self):
        assert BINARY.is_file(), f"build castord first; missing {BINARY}"
        with self.log.open("ab") as output:
            # Existing daemon launch syntax: no unsupported CLI flag makes
            # startup itself RED. Evidence socket is expected beside agent.sock.
            self.process = subprocess.Popen(
                [
                    str(BINARY),
                    "--storage-root",
                    str(self.state),
                    "--socket",
                    str(self.agent),
                    "--control-socket",
                    str(self.control),
                ],
                stdout=output,
                stderr=output,
                env={**os.environ, "CASTORD_EVIDENCE_TRUST_CONFIG": str(self.trust)},
            )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(
                    f"castord exited during startup: {self.log.read_text()}"
                )
            try:
                response = self.call("GetProjectionSummary", {}, channel="control")
                if response.get("status") == "Ok":
                    return
            except (OSError, AssertionError):
                time.sleep(0.01)
        raise AssertionError(f"castord startup timeout: {self.log.read_text()}")

    def kill(self):
        assert self.process is not None and self.process.poll() is None
        self.process.send_signal(signal.SIGKILL)
        assert self.process.wait(timeout=5) == -signal.SIGKILL
        self.process = None

    def restart(self):
        self.kill()
        self.start()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self.temp.cleanup()

    def call(self, op, payload, channel="agent"):
        path = getattr(self, channel)
        if channel == "evidence":
            assert path.exists(), (
                "R2/R4/R5: dedicated authenticated evidence.sock is absent "
                "(no guest fallback)"
            )
        request = encoded({"request_id": "contract", "op": op, "payload": payload})
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(5)
            stream.connect(str(path))
            stream.sendall(struct.pack(">I", len(request)) + request)
            size = struct.unpack(">I", recv_exact(stream, 4))[0]
            assert 0 < size <= 4 * 1024 * 1024, f"invalid response size {size}"
            response = json.loads(recv_exact(stream, size))
        assert response["request_id"] == "contract"
        return response

    def ok(self, op, payload, expected, channel="agent"):
        return expect(self.call(op, payload, channel), expected)

    def summary(self):
        return self.call("GetProjectionSummary", {}, "control")["outcome"]

    def journal(self):
        return self.call("InspectJournal", {}, "control")["outcome"]["entries"]

    def region(self, name, content):
        data = content if isinstance(content, bytes) else encoded(content)
        ref = "region://recovery/" + name
        self.ok(
            "EnsureRegion",
            {"region_ref": ref, "content_digest": digest(data), "content": list(data)},
            "Success",
        )
        return ref, digest(data)

    def prepare(self):
        grant = {
            "cap_id": CAP,
            "subject": AGENT,
            "object_ref": ADAPTER,
            "rights": ["AdmitTurn", "RegisterAction"],
            "constraints": [],
            "parent_cap_id": None,
            "revocation_domain": None,
            "delegation_allowed": False,
            "max_turns": None,
        }
        self.ok("GrantCapability", {"grant": grant}, "CapabilityGranted", "control")
        self.observation = self.region("observation", b"")
        self.manifest = self.region("manifest", b"a1\na2\na3\n")
        self.ok("AdmitTurn", self.admit_payload(1), "Admitted")
        self.ok(
            "RequestInteraction",
            {
                "interaction_id": "initial",
                "lease_epoch": 0,
                "request_digest": digest(b"initial"),
            },
            "InteractionRequested",
        )
        self.report("initial", self.observation)
        self.ok(
            "ConsumeInteraction",
            {"interaction_id": "initial", "lease_epoch": 1},
            "InteractionConsumed",
        )
        self.ok("CommitTurn", self.commit_payload(1), "TurnCommitted")
        for action in ("a1", "a2", "a3"):
            self.ok(
                "RegisterAction",
                {
                    "action_id": action,
                    "stable_operation_id": OP_ID,
                    "agent_id": AGENT,
                    "action_family": ADAPTER,
                    "cap_id": CAP,
                    "target_scope": SCOPE
                    if action != "a3"
                    else "payment:fixture:other",
                },
                "ActionRegistered",
            )

    def admit_payload(self, turn):
        return {
            "agent_id": AGENT,
            "turn_id": turn,
            "lease_epoch": 0,
            "base_projection_digest": self.base,
            "cap_id": CAP,
        }

    def commit_payload(self, lease):
        return {
            "lease_epoch": lease,
            "base_projection_digest": self.base,
            "successor_region_id": self.observation[0],
            "successor_digest": self.observation[1],
            "action_manifest_region_id": self.manifest[0],
            "action_manifest_digest": self.manifest[1],
            "action_manifest": ["a1", "a2", "a3"],
            "cap_id": CAP,
        }

    def admission(self, action="a1", scope=SCOPE):
        return {
            "action_id": action,
            "target_scope": scope,
            "capability_id": CAP,
            "generation": self.generation,
        }

    def arm(self, dispatch=True):
        self.ok("PresentAdmissionCertificate", self.admission(), "AttemptArmed")
        if dispatch:
            self.ok(
                "RecordDispatchAttempt",
                {"attempt_id": 1, "dispatch_identity": OP_ID},
                "DispatchRecorded",
            )

    def durable_projection_digest(self):
        # Existing read-only C-01 proof format; never guest-minted authority.
        data = (self.state / "core-journal.log").read_bytes()
        offset, projection = 0, digest(b"")
        while offset < len(data):
            size = struct.unpack_from("<I", data, offset)[0]
            payload = data[offset + 4 : offset + 4 + size]
            crc = struct.unpack_from("<I", data, offset + 4 + size)[0]
            assert zlib.crc32(payload) == crc, "invalid fixture journal CRC"
            record = json.loads(payload)
            entry = record["request"]["entry"]
            tag = next(iter(entry))
            if tag == "TurnCommitted":
                projection = entry[tag]["successor_projection_digest"]
            elif tag in {
                "ActionRegistered",
                "AttemptArmed",
                "DispatchAttempt",
                "AttemptSettled",
                "QuarantinedDispute",
                "QuarantinedDisputeResolved",
                "AdapterReservation",
                "AdapterSubmissionRecorded",
            }:
                projection = record["proof"]["entry_digest"]
            offset += size + 8
        return projection

    def next_turn(self):
        self.base = self.durable_projection_digest()
        self.turn += 1
        return self.ok("AdmitTurn", self.admit_payload(self.turn), "Admitted")

    def probe(self, interaction="probe", descriptor=None, lease=0):
        if descriptor is None:
            descriptor = {
                "type": "QueryOperation",
                "attempt_id": 1,
                "stable_operation_id": OP_ID,
                "adapter_id": ADAPTER,
            }
        return self.call(
            "RequestInteraction",
            {
                "interaction_id": interaction,
                "lease_epoch": lease,
                "request_digest": digest(encoded(descriptor)),
                "descriptor": descriptor,
            },
        )

    def report(self, interaction, region):
        return self.ok(
            "ReportOutcome",
            {
                "interaction_id": interaction,
                "observation_region_id": region[0],
                "observation_digest": region[1],
            },
            "InteractionBound",
        )

    def certificate(self, resolution="Confirmed", name="receipt"):
        receipt = self.actuator.receipt(resolution)
        ref, sha = self.region(name, receipt)
        return {
            **receipt,
            "dispatch_identity": OP_ID,
            "evidence_region_id": ref,
            "evidence_digest": sha,
            "proof_class": "ProviderConfirmation"
            if resolution == "Confirmed"
            else "VerifiableNonExecution",
        }

    def settle(self, cert):
        return self.call("PresentSettlementCertificate", cert, "evidence")

    def snapshot(self, admitted, status):
        snapshot = admitted.get("unsettled_effects_snapshot")
        assert isinstance(snapshot, dict), (
            "R6/R9: AdmitTurn omitted Core-authored UnsettledEffectsSnapshot: "
            f"{admitted}"
        )
        assert snapshot.get("author") == "Core", snapshot
        assert snapshot.get("turn_id") == self.turn, snapshot
        assert snapshot.get("region_ref"), snapshot
        attempts = snapshot.get("attempts", [])
        item = next((a for a in attempts if a.get("attempt_id") == 1), None)
        assert item is not None, snapshot
        assert item["action_id"] == "a1" and item["target_scope"] == SCOPE, item
        assert item["status"] == status and item["stable_op_id"] == OP_ID, item
        assert item["lock_state"] == "WriteLocked_ProbeAllowed", item
        return snapshot

    def recovery(self):
        recovery = self.summary().get("recovery")
        assert isinstance(recovery, dict), (
            "R3/R10: journal-backed recovery phase/budget projection is missing"
        )
        return recovery


class CognitiveRecovery(unittest.TestCase):
    def fixture(self):
        daemon = Daemon()
        self.addCleanup(daemon.close)
        daemon.prepare()
        return daemon

    def test_r1_guest_certificate_denied_without_journal_mutation(self):
        d = self.fixture()
        d.arm()
        cert = d.certificate()  # persisted valid SHA; no physical commit
        before, projection = d.journal(), d.summary()
        response = d.call("PresentSettlementCertificate", cert)
        with self.subTest("opcode"):
            expect(response, "UnauthorizedOpcode")
        with self.subTest("journal"):
            self.assertEqual(
                d.journal(), before, "guest certificate changed Core journal"
            )
        with self.subTest("scope"):
            self.assertEqual(
                d.summary(), projection, "guest certificate unlocked attempt"
            )
        self.assertEqual(d.actuator.count(), 0)

    def test_r2_reject_mismatched_binding_or_issuer(self):
        for field, wrong in (
            ("attempt_id", 999),
            ("stable_operation_id", "different-op"),
            ("issuer", "untrusted-guest"),
            ("request_digest", digest(b"wrong")),
            ("settlement_schema_version", 999),
            ("adapter_id", "wrong-adapter"),
        ):
            with self.subTest(field=field):
                d = self.fixture()
                d.arm()
                d.actuator.arrive()
                cert = d.certificate()
                cert[field] = wrong
                before = d.journal()
                expect(d.settle(cert), "RejectedBindingOrIssuer")
                self.assertEqual(d.journal(), before)
                self.assertEqual(d.summary()["locked_scopes"], 1)

    def test_r3_not_found_preserves_ambiguity_then_late_dispatch(self):
        d = self.fixture()
        d.arm()
        d.restart()
        admitted = d.next_turn()
        self.assertEqual(d.actuator.query(), "not_found")
        expect(d.probe(), "InteractionRequested")
        d.report(
            "probe",
            d.region(
                "not-found", {"status": "not_found", "stable_operation_id": OP_ID}
            ),
        )
        self.assertEqual(d.summary()["locked_scopes"], 1)
        self.assertFalse(any("AttemptSettled" in e for e in d.journal()))
        d.ok("PresentAdmissionCertificate", d.admission("a2"), "RejectedCurrentState")
        self.assertEqual(d.actuator.arrive(), "Committed")
        self.assertEqual(d.actuator.arrive(), "Committed")
        self.assertEqual(d.actuator.count(), 1)
        # Check the kernel recovery view too; mock correctness alone cannot pass.
        d.snapshot(admitted, "Dispatched")
        self.assertTrue(
            admitted["unsettled_effects_snapshot"]["attempts"][0]["ambiguous_delivery"]
        )
        expect(d.settle(d.certificate()), "Settled")
        self.assertEqual(d.summary()["locked_scopes"], 0)
        self.assertEqual(d.actuator.count(), 1)

    def test_r4_duplicate_confirmed_ack_no_append_no_rearm(self):
        d = self.fixture()
        d.arm()
        d.actuator.arrive()
        cert = d.certificate()
        expect(d.settle(cert), "Settled")
        before, summary = d.journal(), d.summary()
        for _ in range(3):
            expect(d.settle(cert), "Settled")
            self.assertEqual(d.journal(), before)
            self.assertEqual(d.summary(), summary)
        self.assertEqual(sum("AttemptSettled" in e for e in before), 1)
        d.ok("PresentAdmissionCertificate", d.admission(), "RejectedCurrentState")
        self.assertEqual(d.actuator.count(), 1)

    def test_r5_atomic_cancel_tombstone_rejects_delayed_arrivals(self):
        d = self.fixture()
        d.arm()
        self.assertEqual(d.actuator.query(), "not_found")
        d.actuator.cancel()  # existing-op recovery mutation, never a new arm
        self.assertEqual(d.summary()["locked_scopes"], 1)
        for _ in range(2):
            self.assertEqual(d.actuator.arrive(), "TerminatedRejected")
        self.assertEqual(d.actuator.count(), 0)
        expect(d.settle(d.certificate("NotApplied")), "Settled")
        self.assertEqual(d.summary()["locked_scopes"], 0)
        self.assertEqual(d.journal()[-1]["AttemptSettled"]["resolution"], "NotApplied")
        d.restart()
        self.assertEqual(d.actuator.arrive(), "TerminatedRejected")
        self.assertEqual(d.actuator.count(), 0)
        d.ok("RevokeCapability", {"capability_id": CAP}, "CapabilityRevoked", "control")
        d.ok("PresentAdmissionCertificate", d.admission("a2"), "RejectedCapabilityRevoked")

    def test_r6_next_turn_probe_allowed_mutation_locked(self):
        d = self.fixture()
        d.arm(dispatch=False)
        admitted = d.next_turn()
        d.ok("PresentAdmissionCertificate", d.admission("a2"), "RejectedCurrentState")
        with self.subTest("unrelated scope remains available"):
            d.ok(
                "PresentAdmissionCertificate",
                d.admission("a3", "payment:fixture:other"),
                "AttemptArmed",
            )
        expect(d.probe(), "InteractionRequested")
        # A probe surrenders the old lease; it cannot authorize a domain commit.
        d.ok("CommitTurn", d.commit_payload(0), "RejectedStaleAuthority")
        d.snapshot(admitted, "ArmedUnknown")

    def test_r7_revoked_capability_after_snapshot_rejected(self):
        d = self.fixture()
        d.arm()
        admitted = d.next_turn()
        d.ok("RevokeCapability", {"capability_id": CAP}, "CapabilityRevoked", "control")
        before = d.journal()
        response = d.call(
            "PresentAdmissionCertificate",
            {**d.admission("a3", "payment:fixture:other"), "snapshot": admitted},
        )
        with self.subTest("revoked authority rejection"):
            self.assertIn(kind(response), {"RejectedPrecondition", "RejectedCapabilityRevoked"})
        self.assertEqual(d.journal(), before)
        self.assertEqual(d.summary()["locked_scopes"], 1, "revoke must not unarm")
        expect(d.probe(), "InteractionRequested")  # observational probe survives revoke

    def test_r8_closed_query_descriptor_validation(self):
        valid = {
            "type": "QueryOperation",
            "attempt_id": 1,
            "stable_operation_id": OP_ID,
            "adapter_id": ADAPTER,
        }
        invalid = [
            {**valid, "url": "http://127.0.0.1:9/admin"},
            {**valid, "shell": "touch /tmp/forbidden"},
            {**valid, "sql": "DELETE FROM payments"},
            {**valid, "target_scope": "foreign-scope"},
            {**valid, "attempt_id": "not-an-integer"},
            {**valid, "attempt_id": 999},
            {**valid, "stable_operation_id": "wrong"},
            {**valid, "adapter_id": "unregistered"},
            {"type": "QueryOperation"},
        ]
        for descriptor in invalid:
            with self.subTest(descriptor=descriptor):
                d = self.fixture()
                d.arm()
                d.next_turn()
                before = d.journal()
                response = d.probe(descriptor=descriptor)
                with self.subTest("error"):
                    expect(response, "RejectedPrecondition")
                self.assertEqual(
                    d.journal(), before, "malformed probe consumed authority/journal"
                )
        d = self.fixture()
        expect(d.call("UnknownRecoveryOpcode", {}), "UnauthorizedOpcode")

    def test_r9_sigkill_arm_dispatch_evidence_and_torn_fsync(self):
        for seam in ("arm", "dispatch", "evidence", "torn-settlement"):
            with self.subTest(seam=seam):
                d = self.fixture()
                d.arm(dispatch=seam != "arm")
                if seam in ("evidence", "torn-settlement"):
                    # Raw region persisted, never authoritative settlement.
                    d.certificate()
                before = d.journal()
                d.kill()
                journal = d.state / "core-journal.log"
                original_size = journal.stat().st_size
                if seam == "torn-settlement":
                    # Deterministic uncommitted settlement tail, after real SIGKILL.
                    # Inherited D1 length/payload/CRC framing. Not a claim of
                    # hitting the interior of fsync or simulating power loss.
                    payload = encoded(
                        {
                            "entry": {
                                "AttemptSettled": {
                                    "attempt_id": 1,
                                    "resolution": "Confirmed",
                                }
                            }
                        }
                    )
                    with journal.open("ab") as f:
                        f.write(
                            struct.pack("<I", len(payload))
                            + payload[: len(payload) // 2]
                        )
                        f.flush()
                        os.fsync(f.fileno())
                d.start()
                self.assertEqual(
                    d.journal(), before, "replay changed committed history"
                )
                self.assertEqual(
                    journal.stat().st_size, original_size, "torn tail not truncated"
                )
                self.assertEqual(d.summary()["locked_scopes"], 1)
                d.ok("CommitTurn", d.commit_payload(1), "RejectedStaleAuthority")
                if seam != "arm":
                    d.ok(
                        "DeliverArmedAttempt",
                        {"attempt_id": 1, "dispatch_identity": OP_ID},
                        "Ambiguous",
                    )
                admitted = d.next_turn()
                d.snapshot(admitted, "ArmedUnknown" if seam == "arm" else "Dispatched")

    def test_r10_budget_survives_restart_escalation_requires_hitl(self):
        d = self.fixture()
        d.arm()
        d.next_turn()
        initial = d.recovery()
        remaining = initial["probe_budget_remaining"]
        self.assertGreater(remaining, 0)
        self.assertLessEqual(
            remaining, 32, "bounded RED fixture must not issue unbounded probes"
        )
        lease = 0
        for index in range(remaining):
            interaction = f"budget-{index}"
            expect(d.probe(interaction, lease=lease), "InteractionRequested")
            d.report(interaction, d.region(interaction, {"status": "not_found"}))
            after = d.recovery()
            self.assertEqual(after["probe_budget_remaining"], remaining - index - 1)
            d.restart()
            self.assertEqual(
                d.recovery()["probe_budget_remaining"], after["probe_budget_remaining"]
            )
            if index < remaining - 1:
                lease += 1
                d.ok(
                    "ConsumeInteraction",
                    {"interaction_id": interaction, "lease_epoch": lease},
                    "InteractionConsumed",
                )
        self.assertEqual(d.recovery()["phase"], "Escalated")
        before = d.journal()
        expect(d.probe("exhausted", lease=lease), "RejectedCurrentState")
        d.actuator.arrive()  # late true receipt still cannot autonomously unlock
        expect(d.settle(d.certificate()), "RejectedCurrentState")
        self.assertEqual(d.journal(), before)
        self.assertEqual(d.summary()["locked_scopes"], 1)
        decision = {
            "attempt_id": 1,
            "decision": "RearmEvidence",
            "operator_id": "test-operator",
        }
        expect(d.call("SubmitDecision", decision), "UnauthorizedOpcode")
        reply = d.call("SubmitDecision", decision, "control")
        self.assertEqual(reply["status"], "Ok", reply)
        self.assertNotIn(
            kind(reply),
            ("UnauthorizedOpcode", "RejectedPrecondition", "RejectedCurrentState"),
        )
        self.assertGreater(
            len(d.journal()), len(before), "operator decision must be durable"
        )

    def test_r12_closed_turn_drops_late_probe_requires_fresh_lease(self):
        d = self.fixture()
        d.arm()
        d.next_turn()
        expect(d.probe("late"), "InteractionRequested")
        # PersistFence is a real control-plane Turn close, not a test opcode.
        d.ok("PersistFence", {"generation": 2}, "GenerationFenced", "control")
        d.generation = 2
        region = d.region("late-receipt", {"status": "not_found"})
        before = d.journal()
        expect(
            d.call(
                "ReportOutcome",
                {
                    "interaction_id": "late",
                    "observation_region_id": region[0],
                    "observation_digest": region[1],
                },
            ),
            "RejectedLateOrClosedTurn",
        )
        self.assertEqual(d.journal(), before)
        d.ok(
            "ConsumeInteraction",
            {"interaction_id": "late", "lease_epoch": 0},
            "RejectedStaleAuthority",
        )
        # Renew capability after generation fence before admitting t+2.
        d.ok(
            "GrantCapability",
            {
                "grant": {
                    "cap_id": "fresh-cap",
                    "subject": AGENT,
                    "object_ref": ADAPTER,
                    "rights": ["AdmitTurn", "RegisterAction"],
                    "constraints": [],
                    "parent_cap_id": None,
                    "revocation_domain": None,
                    "delegation_allowed": False,
                    "max_turns": None,
                }
            },
            "CapabilityGranted",
            "control",
        )
        d.turn += 1
        d.ok(
            "AdmitTurn", {**d.admit_payload(d.turn), "cap_id": "fresh-cap"}, "Admitted"
        )
        old_budget = d.recovery()["probe_budget_remaining"]
        expect(d.probe("fresh"), "InteractionRequested")
        d.report("fresh", d.region("fresh-result", {"status": "not_found"}))
        d.ok(
            "ConsumeInteraction",
            {"interaction_id": "fresh", "lease_epoch": 0},
            "RejectedStaleAuthority",
        )
        d.ok(
            "ConsumeInteraction",
            {"interaction_id": "fresh", "lease_epoch": 1},
            "InteractionConsumed",
        )
        self.assertEqual(d.recovery()["probe_budget_remaining"], old_budget - 1)


if __name__ == "__main__":
    unittest.main()
