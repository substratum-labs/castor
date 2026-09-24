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
    assert not any(
        name.startswith(
            ("castor/gate/", "castor/kernel/", "castor/scheduler/", "castor/mcp/")
        )
        for name in names
    )

    environment = {**os.environ, "PYTHONPATH": str(SHIM_WHEEL)}
    probe = subprocess.run(
        [sys.executable, "-c", "from castor import Castor; Castor()"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0
    assert "castor-client" in probe.stderr
