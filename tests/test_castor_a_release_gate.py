"""The supported Castor A wheels must not install the Python authority engine."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_WHEEL = (
    ROOT / "packages/castor-client/dist/castor_client-0.7.0a1-py3-none-any.whl"
)


def test_client_wheel_has_no_python_authority_engine() -> None:
    assert CLIENT_WHEEL.is_file(), "build the Castor A client wheel first"
    with zipfile.ZipFile(CLIENT_WHEEL) as wheel:
        names = set(wheel.namelist())
    assert "castor_client/session.py" in names
    assert {
        name for name in names if name.startswith("castor/") and name.endswith(".py")
    } == set()

    source = (
        "import importlib.util, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "from castor_client import AgentSession, OperatorSession; "
        "assert importlib.util.find_spec('castor') is None"
    )
    probe = subprocess.run(
        [sys.executable, "-I", "-S", "-c", source, str(CLIENT_WHEEL)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
