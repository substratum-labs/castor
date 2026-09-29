"""Trusted Linux management controller for the existing native project frontend."""

import json
import os
import subprocess
import sys
from pathlib import Path

from common import write_json
from mock_model import MockModel
from real_model import FileBridgeModel


def main():
    config = json.loads(Path("/state/launcher/controller-config.json").read_text())
    evidence = Path("/state/launcher")
    model = (
        FileBridgeModel(os.environ["CASTOR_MODEL_SOCKET"], evidence, config.get("deadline_seconds", 300))
        if config.get("model_mode") in ("ollama", "file_bridge_fixture")
        else MockModel(os.environ["CASTOR_MODEL_SOCKET"], config["mock_mode"], evidence)
    )
    try:
        # Native Castor alone packages the target and runs its isolated oracle.
        result = subprocess.run(
            [
                "/native/castor",
                "run",
                "--project",
                "/project",
                "--task-spec",
                "/state/launcher/task-spec.json",
            ],
            check=False,
        )
        write_json(evidence / "native-exit.json", {"exit_code": result.returncode})
        return result.returncode
    finally:
        model.close()


if __name__ == "__main__":
    sys.exit(main())
