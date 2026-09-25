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

from tests.test_cognitive_recovery_castord import ADAPTER, AGENT, CAP, Daemon

ROOT = Path(__file__).resolve().parents[1]
WHEEL = ROOT / "packages/castor-client/dist/castor_client-0.7.0a1-py3-none-any.whl"
FIXTURE = ROOT / "tests/fixtures/castor_a_installed_agent.py"
DOCKERFILE = ROOT / "tests/fixtures/Dockerfile.castor_a_client"
RUN_PHYSICAL = (
    sys.platform == "linux" and os.environ.get("CASTOR_A_ROCHE_PHYSICAL") == "1"
)


@unittest.skipUnless(RUN_PHYSICAL, "requires the Linux Roche physical CI job")
class CastorARochePhysical(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not WHEEL.is_file():
            raise AssertionError("build the Castor A client wheel first")
        cls.image = f"castor-a-client-{uuid.uuid4().hex[:12]}:test"
        with tempfile.TemporaryDirectory(prefix="castor-a-image-") as directory:
            context = Path(directory)
            shutil.copy2(WHEEL, context / WHEEL.name)
            shutil.copy2(FIXTURE, context / FIXTURE.name)
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
                [
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
