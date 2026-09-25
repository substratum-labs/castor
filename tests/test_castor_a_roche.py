"""Linux physical Roche-profile socket and installed-wheel contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.dogfood.repo_workspace_actuator import (
    ACTUATOR_ID,
    ActuatorConfig,
    RepoWorkspaceActuator,
    canonical_json,
)
from tests.fixtures.castor_a_trace_support import (
    prepare_trace,
    report_model_after_request,
)
from tests.test_cognitive_recovery_castord import (
    ADAPTER,
    AGENT,
    CAP,
    SIGNING_KEY,
    Daemon,
    expect,
)

ROOT = Path(__file__).resolve().parents[1]
WHEEL = ROOT / "packages/castor-client/dist/castor_client-0.7.0a1-py3-none-any.whl"
FIXTURE = ROOT / "tests/fixtures/castor_a_installed_agent.py"
HOSTILE_FIXTURE = ROOT / "tests/fixtures/castor_a_hostile_agent.py"
DOCKERFILE = ROOT / "tests/fixtures/Dockerfile.castor_a_client"
RUN_PHYSICAL = (
    sys.platform == "linux" and os.environ.get("CASTOR_A_ROCHE_PHYSICAL") == "1"
)


@unittest.skipUnless(RUN_PHYSICAL, "requires the Linux Roche physical CI job")
class CastorARochePhysical(unittest.TestCase):
    def container_args(self, name: str, daemon: Daemon) -> list[str]:
        return [
            "docker",
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--pids-limit",
            "256",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--mount",
            f"type=bind,src={daemon.agent},dst=/run/castor/ipc.sock,readonly",
            "--env",
            "CASTOR_IPC_SOCKET=/run/castor/ipc.sock",
        ]

    def run_full_guest(
        self, daemon: Daemon, *, action_payload: bytes = b"payload-a1"
    ) -> list[str]:
        name = f"castor-a-roche-{uuid.uuid4().hex[:12]}"
        process: subprocess.Popen[str] | None = None
        try:
            config, observation = prepare_trace(daemon, action_payload=action_payload)
            config["socket"] = "/run/castor/ipc.sock"
            process = subprocess.Popen(
                self.container_args(name, daemon)
                + [
                    "--env",
                    f"CASTOR_A_TRACE_CONFIG={json.dumps(config)}",
                    self.image,
                    "python",
                    "-B",
                    "-I",
                    "/opt/castor/agent.py",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = report_model_after_request(
                daemon, process, str(config["interaction_id"]), observation
            )
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
            return outcomes
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            subprocess.run(
                ["docker", "rm", "--force", name],
                capture_output=True,
                text=True,
                check=False,
            )

    @classmethod
    def setUpClass(cls) -> None:
        if not WHEEL.is_file():
            raise AssertionError("build the Castor A client wheel first")
        cls.image = f"castor-a-client-{uuid.uuid4().hex[:12]}:test"
        with tempfile.TemporaryDirectory(prefix="castor-a-image-") as directory:
            context = Path(directory)
            shutil.copy2(WHEEL, context / WHEEL.name)
            shutil.copy2(FIXTURE, context / FIXTURE.name)
            shutil.copy2(HOSTILE_FIXTURE, context / HOSTILE_FIXTURE.name)
            shutil.copy2(DOCKERFILE, context / "Dockerfile")
            build = subprocess.run(
                ["docker", "build", "--tag", cls.image, str(context)],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            if build.returncode != 0:
                raise AssertionError(f"Roche Agent image build failed: {build.stderr}")

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["docker", "image", "rm", "--force", cls.image],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_nonroot_roche_guest_can_admit_through_its_only_socket(self) -> None:
        daemon = Daemon(sandbox=True)
        name = f"castor-a-roche-{uuid.uuid4().hex[:12]}"
        try:
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
            daemon.ok(
                "GrantCapability", {"grant": grant}, "CapabilityGranted", "control"
            )
            source = (
                "import json,sys; "
                "from castor_client import AgentSession; "
                "print(json.dumps(AgentSession('/run/castor/ipc.sock').request("
                "'AdmitTurn', json.loads(sys.argv[1]))['outcome']))"
            )
            admit = {
                "agent_id": AGENT,
                "turn_id": 1,
                "lease_epoch": 0,
                "base_projection_digest": daemon.base,
                "cap_id": CAP,
            }
            run = subprocess.run(
                self.container_args(name, daemon)
                + [
                    self.image,
                    "python",
                    "-B",
                    "-I",
                    "-c",
                    source,
                    json.dumps(admit),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["type"], "Admitted")
            inspect = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True,
                text=True,
                check=True,
            )
            profile = json.loads(inspect.stdout)[0]
            self.assertEqual(profile["HostConfig"]["NetworkMode"], "none")
            self.assertTrue(profile["HostConfig"]["ReadonlyRootfs"])
            self.assertEqual(profile["HostConfig"]["PidsLimit"], 256)
            self.assertIn("ALL", profile["HostConfig"]["CapDrop"])
            self.assertFalse(profile["HostConfig"]["CapAdd"])
            self.assertFalse(profile["HostConfig"]["Privileged"])
            self.assertTrue(
                any(
                    option.startswith("no-new-privileges")
                    for option in profile["HostConfig"]["SecurityOpt"]
                )
            )
            self.assertEqual(profile["Config"]["User"], "10001:10001")
            self.assertEqual(len(profile["Mounts"]), 1)
            self.assertEqual(profile["Mounts"][0]["Source"], str(daemon.agent))
            self.assertEqual(
                profile["Mounts"][0]["Destination"], "/run/castor/ipc.sock"
            )
            self.assertFalse(profile["Mounts"][0]["RW"])
        finally:
            subprocess.run(
                ["docker", "rm", "--force", name],
                capture_output=True,
                text=True,
                check=False,
            )
            daemon.close()

    def test_nonroot_roche_guest_completes_governed_turn(self) -> None:
        daemon = Daemon(sandbox=True)
        try:
            self.run_full_guest(daemon)
            delivery = daemon.acquire()
            self.assertEqual(delivery["delivery_outcome"], "Delivered")
            self.assertEqual(daemon.actuator.arrive(), "Committed")
            expect(daemon.settle(daemon.certificate()), "Settled")
            self.assertEqual(daemon.actuator.count(), 1)
            journal = daemon.journal()
            daemon.restart()
            self.assertEqual(daemon.journal(), journal)
            self.assertEqual(daemon.actuator.count(), 1)
        finally:
            daemon.close()

    def delivery_fault_trace(
        self,
        fault_point: str,
        expected_exit: int,
        submission_persisted: bool,
        expected_delivery: str,
    ) -> None:
        daemon = Daemon(sandbox=True, fault_point=fault_point)
        try:
            self.run_full_guest(daemon)
            self.assertEqual(daemon.actuator.count(), 0)
            with self.assertRaises((AssertionError, OSError)):
                daemon.acquire()
            self.assertEqual(daemon.process.wait(timeout=5), expected_exit)

            daemon.fault_point = None
            daemon.start()
            journal = daemon.journal()
            self.assertEqual(
                sum("AdapterSubmissionRecorded" in entry for entry in journal),
                int(submission_persisted),
            )
            delivery = daemon.acquire()
            self.assertEqual(delivery["delivery_outcome"], expected_delivery)
            self.assertEqual(bytes(delivery["payload"]), b"payload-a1")
            self.assertEqual(
                sum("AdapterSubmissionRecorded" in entry for entry in daemon.journal()),
                1,
            )
            self.assertEqual(daemon.actuator.arrive(), "Committed")
            expect(daemon.settle(daemon.certificate()), "Settled")
            self.assertEqual(daemon.actuator.count(), 1)
            self.assertEqual(daemon.summary()["locked_scopes"], 0)
        finally:
            daemon.close()

    def test_c1_pre_delivery_append_crash_retries_without_submission(self) -> None:
        self.delivery_fault_trace("before_delivery_append", 86, False, "Delivered")

    def test_c2_post_fsync_lost_reply_is_duplicate_delivery(self) -> None:
        self.delivery_fault_trace(
            "after_delivery_fsync_before_response", 87, True, "DuplicateDelivery"
        )

    def test_d1_duplicate_delivery_and_ack_do_not_reapply(self) -> None:
        daemon = Daemon(sandbox=True)
        try:
            self.run_full_guest(daemon)
            first = daemon.acquire()
            self.assertEqual(first["delivery_outcome"], "Delivered")
            journal = daemon.journal()
            duplicate = daemon.acquire()
            self.assertEqual(duplicate["delivery_outcome"], "DuplicateDelivery")
            self.assertEqual(duplicate["payload"], first["payload"])
            self.assertEqual(daemon.journal(), journal)
            self.assertEqual(daemon.actuator.arrive(), "Committed")
            self.assertEqual(daemon.actuator.arrive(), "Committed")
            self.assertEqual(daemon.actuator.count(), 1)
            certificate = daemon.certificate()
            expect(daemon.settle(certificate), "Settled")
            settled_journal = daemon.journal()
            expect(daemon.settle(certificate), "Settled")
            self.assertEqual(daemon.journal(), settled_journal)
            daemon.ok(
                "PresentAdmissionCertificate",
                daemon.admission(),
                "RejectedCurrentState",
            )
            self.assertEqual(daemon.journal(), settled_journal)
            daemon.restart()
            self.assertEqual(daemon.actuator.count(), 1)
            self.assertEqual(daemon.summary()["locked_scopes"], 0)
        finally:
            daemon.close()

    def test_h1_hostile_guest_cannot_escape_agent_channel(self) -> None:
        daemon = Daemon(sandbox=True)
        name = f"castor-a-hostile-{uuid.uuid4().hex[:12]}"
        try:
            before = daemon.journal()
            projection = daemon.summary()
            run = subprocess.run(
                self.container_args(name, daemon)
                + [self.image, "python", "-B", "-I", "/opt/castor/hostile.py"],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), ["UnauthorizedOpcode"] * 3)
            self.assertEqual(daemon.journal(), before)
            self.assertEqual(daemon.summary(), projection)
            self.assertEqual(daemon.actuator.count(), 0)
        finally:
            subprocess.run(
                ["docker", "rm", "--force", name],
                capture_output=True,
                text=True,
                check=False,
            )
            daemon.close()

    def test_c3_file_replace_crash_reconciles_without_second_effect(self) -> None:
        relative = "src/castor/ipc_client.py"
        target_scope = f"repo:castor:file/{relative}"
        payload = canonical_json(
            {"kind": "write_file", "path": relative, "content_utf8": "recovered\n"}
        )
        daemon = Daemon(sandbox=True, adapter_id=ACTUATOR_ID, target_scope=target_scope)
        try:
            with tempfile.TemporaryDirectory(
                prefix="castor-a-target-", dir="/tmp"
            ) as root:
                target = Path(root)
                path = target / relative
                path.parent.mkdir(parents=True)
                path.write_text("old\n")
                self.run_full_guest(daemon, action_payload=payload)
                envelope = daemon.acquire()
                self.assertEqual(envelope["delivery_outcome"], "Delivered")
                self.assertEqual(bytes(envelope["payload"]), payload)
                config = ActuatorConfig(
                    target_workspace=target,
                    run_dir=daemon.root,
                    state_db=daemon.root / "workspace-actuator.sqlite",
                    actuator_socket=daemon.delivery,
                    evidence_socket=daemon.evidence,
                    actuator_secret=SIGNING_KEY,
                    issuer="fixture-evidence-service",
                )
                script = """import json, os, sys
from pathlib import Path
from tests.dogfood.repo_workspace_actuator import ActuatorConfig, RepoWorkspaceActuator
config = ActuatorConfig(
    target_workspace=Path(sys.argv[1]), run_dir=Path(sys.argv[2]),
    state_db=Path(sys.argv[3]), actuator_socket=Path(sys.argv[4]),
    evidence_socket=Path(sys.argv[5]), actuator_secret=bytes.fromhex(sys.argv[6]),
    issuer='fixture-evidence-service')
def crash(phase):
    if phase == 'after_file_replace':
        os._exit(93)
RepoWorkspaceActuator(config, crash_hook=crash).process_envelope(
    json.loads(sys.argv[7]))
raise AssertionError('the physical crash hook did not run')
"""
                crash = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(config.target_workspace),
                        str(config.run_dir),
                        str(config.state_db),
                        str(config.actuator_socket),
                        str(config.evidence_socket),
                        SIGNING_KEY.hex(),
                        json.dumps(envelope),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(crash.returncode, 93, crash.stderr)
                self.assertEqual(path.read_text(), "recovered\n")
                inode = path.stat().st_ino
                with mock.patch(
                    "tests.dogfood.repo_workspace_actuator.os.replace"
                ) as replace:
                    actuator = RepoWorkspaceActuator(config)
                    result = actuator.process_envelope(envelope)
                replace.assert_not_called()
                self.assertEqual(path.stat().st_ino, inode)
                self.assertEqual(path.read_text(), "recovered\n")
                self.assertEqual(result["physical_observation"], "reconciled_matching")

                def publish(ref: str, content_digest: str, content: bytes) -> None:
                    daemon.ok(
                        "EnsureRegion",
                        {
                            "region_ref": ref,
                            "content_digest": content_digest,
                            "content": list(content),
                        },
                        "Success",
                    )

                actuator.settle(result, publish)
                daemon.restart()
                self.assertEqual(path.read_text(), "recovered\n")
                self.assertEqual(path.stat().st_ino, inode)
                self.assertEqual(daemon.summary()["locked_scopes"], 0)
        finally:
            daemon.close()
