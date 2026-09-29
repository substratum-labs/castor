#!/usr/bin/env python3
"""Opt-in management/test launcher; the native Castor runtime is unchanged."""

import argparse
import hashlib
import json
import math
import shutil
import sys
import time
from pathlib import Path

from trusted_slot.lifecycle import Slot
from trusted_slot.real_model import MODEL_DIGEST
from trusted_slot.safe_git import clean_tree
from trusted_slot.common import CONTROLLER, PI, VERIFIER

REFERENCE_SHA256 = "0aeac3312b66b3919ec93f35e9785c62f3d79bf6db82d60f3b5a3140b3d30ae5"
RUNTIME_FILES = ("run_trusted_slot.py",) + tuple(
    "trusted_slot/" + path.name
    for path in sorted((Path(__file__).resolve().parent / "trusted_slot").glob("*.py"))
)


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
    parser.add_argument("--model-mode", choices=("mock", "ollama", "file_bridge_fixture"), default="mock")
    parser.add_argument("--mock-mode", choices=("fixing", "nonfixing", "timeout", "bits_fixture"))
    parser.add_argument("--protocol-file")
    parser.add_argument("--release-file")
    parser.add_argument("--source-manifest")
    parser.add_argument("--acceptance-file")
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--workload-timeout", type=float, default=120)
    parser.add_argument("--cleanup-timeout", type=float, default=60)
    args = parser.parse_args()
    if args.model_mode == "mock" and not args.mock_mode:
        parser.error("mock mode requires --mock-mode")
    if args.model_mode == "file_bridge_fixture" and (args.mock_mode or args.task_id == "task-t372-bits-r1" or args.idempotency_key == "task-t372-bits-r1"):
        parser.error("fake file bridge requires separate preflight identity and no mock mode")
    if args.model_mode == "ollama":
        if args.mock_mode or not args.protocol_file or not args.release_file or not args.source_manifest or not args.acceptance_file:
            parser.error("Ollama requires protocol, source manifest and root release, without mock mode")
        protocol_bytes = Path(args.protocol_file).read_bytes()
        protocol = json.loads(protocol_bytes)
        release = json.loads(Path(args.release_file).read_bytes())
        manifest = json.loads(Path(args.source_manifest).read_bytes())
        acceptance = json.loads(Path(args.acceptance_file).read_bytes())
        spec_hash = hashlib.sha256(Path(args.task_spec).read_bytes()).hexdigest()
        source_hash = hashlib.sha256((Path(args.project) / "boltons/mathutils.py").read_bytes()).hexdigest()
        tests_hash = hashlib.sha256((Path(args.project) / "tests/test_mathutils.py").read_bytes()).hexdigest()
        try:
            tree = clean_tree(args.project)
        except (ValueError, OSError) as error:
            parser.error(str(error))
        binaries = {name: hashlib.sha256((Path(args.linux_bin_dir) / name).read_bytes()).hexdigest() for name in ("castor", "castord")}
        reference = Path(manifest.get("reference", "/nonexistent")).resolve()
        target = Path(args.project).resolve()
        reference_hash = (
            hashlib.sha256((reference / "boltons/mathutils.py").read_bytes()).hexdigest()
            if reference.is_dir() and reference != target and target not in reference.parents
            else None
        )
        wheel_dir = target / "tests/t372_wheels"
        wheel_hashes = {
            name: hashlib.sha256((wheel_dir / name).read_bytes()).hexdigest()
            for name in protocol.get("wheel_sha256", {})
        }
        runtime_root = Path(__file__).resolve().parent
        runtime_hashes = {
            name: hashlib.sha256((runtime_root / name).read_bytes()).hexdigest()
            for name in RUNTIME_FILES
        }
        protocol_hash = hashlib.sha256(protocol_bytes).hexdigest()
        if not (
            args.task_id == args.idempotency_key == "task-t372-bits-r1"
            and args.workload_timeout == 300
            and args.cleanup_timeout == 60
            and acceptance.get("protocol_sha256") == protocol_hash
            and acceptance.get("accepted") is True
            and acceptance.get("scopes") == ["model_bridge", "live_gate"]
            and protocol.get("task_id") == args.task_id
            and protocol.get("model_digest") == MODEL_DIGEST
            and protocol.get("model") == "qwen3.5:9b"
            and protocol.get("ollama_version") == "0.34.1"
            and protocol.get("think") is False
            and protocol.get("temperature") == 0.2
            and protocol.get("seed") == 17
            and protocol.get("num_ctx") == 32768
            and protocol.get("max_unique_http_calls") == 3
            and protocol.get("max_output_tokens_per_call") == 512
            and protocol.get("workload_timeout_seconds") == 300
            and protocol.get("cleanup_timeout_seconds") == 60
            and protocol.get("source_sha256") == source_hash
            and protocol.get("tests_sha256") == tests_hash
            and protocol.get("spec_sha256") == spec_hash
            and protocol.get("target_tree") == tree
            and protocol.get("reference_source_sha256") == REFERENCE_SHA256 == reference_hash
            and manifest.get("reference_source_sha256") == reference_hash
            and manifest.get("target_tree") == tree
            and manifest.get("source_sha256") == source_hash
            and manifest.get("original_regression_tests_sha256") == tests_hash
            and protocol.get("controller_image") == CONTROLLER
            and protocol.get("pi_image") == PI
            and protocol.get("verifier_image") == "sha256:" + VERIFIER.rsplit("sha256:", 1)[1]
            and protocol.get("wheel_sha256") == manifest.get("wheel_sha256") == wheel_hashes
            and protocol.get("runtime_source_sha256") == runtime_hashes
            and protocol.get("native_binaries_sha256") == binaries
            and release.get("task_id") == args.task_id
            and release.get("protocol_sha256") == protocol_hash
            and release.get("model_digest") == MODEL_DIGEST
            and release.get("target_tree") == tree
            and release.get("spec_sha256") == spec_hash
            and release.get("state_root") == str(Path(args.state_root).resolve())
            and release.get("workload_timeout_seconds") == 300
            and release.get("cleanup_timeout_seconds") == 60
            and release.get("max_unique_http_calls") == 3
            and release.get("max_output_tokens_per_call") == 512
            and release.get("budget_approved") is True
            and release.get("root_released") is True
        ):
            parser.error("live gate input/protocol/source hash or root release mismatch")
        args.protocol = protocol
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
