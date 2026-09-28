"""Real pytest discovery checks; no physical containers or target execution."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/test_trusted_launcher.py"


class TrustedLauncherCollection(unittest.TestCase):
    def environment(self):
        env = os.environ.copy()
        env.pop("CASTOR_TRUSTED_LAUNCHER_PHYSICAL", None)
        env["CASTOR_ROOT"] = str(ROOT)
        return env

    def collect(self, env):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(HARNESS)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_default_pytest_does_not_collect_physical_class(self):
        result = self.collect(self.environment())
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertNotIn("TestLauncherHarness::", result.stdout)
        self.assertIn("no tests collected", result.stdout)

    def test_explicit_opt_in_collects_all_six_physical_vectors(self):
        env = self.environment()
        env["CASTOR_TRUSTED_LAUNCHER_PHYSICAL"] = "1"
        result = self.collect(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("TestLauncherHarness::test_v"), 6)

    def test_direct_invocation_fails_missing_prerequisite_without_skip(self):
        env = self.environment()
        env["DOCKER_BIN"] = "/nonexistent/t371-docker"
        result = subprocess.run(
            [
                sys.executable,
                str(HARNESS),
                "TestLauncherHarness.test_v1_normal_full_path",
                "-v",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Docker executable required", result.stderr)
        self.assertIn("FAILED (failures=1)", result.stderr)
        self.assertNotIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
