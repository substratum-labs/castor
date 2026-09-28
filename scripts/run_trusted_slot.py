#!/usr/bin/env python3
"""Opt-in management/test launcher; the native Castor runtime is unchanged."""

import argparse
import json
import math
import shutil
import sys
import time
from pathlib import Path

from trusted_slot.lifecycle import Slot


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "project",
        "task-spec",
        "task-id",
        "idempotency-key",
        "state-root",
        "linux-bin-dir",
        "trusted-controller-image",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--scratch-parent", default="/run")
    parser.add_argument(
        "--mock-mode", choices=("fixing", "nonfixing", "timeout"), required=True
    )
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--workload-timeout", type=float, default=120)
    parser.add_argument("--cleanup-timeout", type=float, default=60)
    args = parser.parse_args()
    if any(
        not math.isfinite(value) or value <= 0
        for value in (args.workload_timeout, args.cleanup_timeout)
    ):
        parser.error("deadlines must be finite and positive")
    for value in (
        args.project,
        args.state_root,
        args.scratch_parent,
        args.linux_bin_dir,
    ):
        if "," in value or "\n" in value or not Path(value).is_absolute():
            parser.error("bind paths must be absolute and contain no comma/newline")
    executable = shutil.which(args.docker_bin)
    if not executable:
        parser.error("Docker executable missing")
    args.docker_bin = str(Path(executable).resolve())
    return args


def main():
    started = time.monotonic()
    args = parse_args()
    slot = Slot(args, started)
    try:
        slot.preflight()
        slot.start()
        slot.run_workload()
    except Exception as error:
        print(str(error), file=sys.stderr)
        if time.monotonic() >= slot.work_deadline:
            slot.status = "TIMEOUT"
    envelope = slot.finish()
    print(json.dumps(envelope))
    return (
        2
        if envelope["cleanup_status"] == "RETAINED_FAILURE"
        else 0
        if envelope["launcher_status"] == "SUCCEEDED"
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
