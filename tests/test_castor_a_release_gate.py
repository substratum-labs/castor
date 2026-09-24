"""The supported Castor A wheels must not install the Python authority engine."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIM_WHEEL = (
    ROOT / "packages/castor-kernel-shim/dist/castor_kernel-0.7.0a1-py3-none-any.whl"
)


def test_migration_wheel_refuses_old_castor_api() -> None:
    assert SHIM_WHEEL.is_file(), "build the Castor A migration wheel first"
    with zipfile.ZipFile(SHIM_WHEEL) as wheel:
        names = set(wheel.namelist())
    assert "castor/__init__.py" in names
    assert {
        name for name in names if name.startswith("castor/") and name.endswith(".py")
    } <= {"castor/__init__.py", "castor/cli.py"}

    environment = {**os.environ, "PYTHONPATH": str(SHIM_WHEEL)}
    probes = (
        "from castor import Castor; Castor()",
        "from castor.scheduler.runner import AgentRunner",
        "from castor.gate.validator import SyscallGate",
        "from castor.kernel.journal import InMemoryJournal",
    )
    for source in probes:
        probe = subprocess.run(
            [sys.executable, "-c", source],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode != 0, source
        assert "castor-client" in probe.stderr, source
