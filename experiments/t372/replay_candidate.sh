#!/usr/bin/env bash
# Rebuild and verify the recorded r2 patch. No agent or model is started.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
CASTOR_REPLAY_ROOT="$(mktemp -d /tmp/castor-t372-replay.XXXXXX)"
CASTOR_REPLAY_ROOT="$(cd "$CASTOR_REPLAY_ROOT" && pwd -P)"
export CASTOR_REPLAY_ROOT
trap 'printf "Replay artifacts: %s\n" "$CASTOR_REPLAY_ROOT"' EXIT

# Use the same immutable image through its fully-qualified repository name.
docker image inspect \
  docker.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea \
  > "$CASTOR_REPLAY_ROOT/image-before.json" 2> "$CASTOR_REPLAY_ROOT/image-preflight.stderr"
docker ps -a --no-trunc --format '{{.ID}}' > "$CASTOR_REPLAY_ROOT/containers-before.txt"
python3 -m pip download --only-binary :all: --no-deps \
  --dest "$CASTOR_REPLAY_ROOT/wheels" \
  pytest==8.3.5 iniconfig==2.0.0 packaging==24.2 pluggy==1.5.0 \
  > "$CASTOR_REPLAY_ROOT/wheel-download.log" 2>&1
python3 experiments/t372/prepare_source.py \
  --wheel-dir "$CASTOR_REPLAY_ROOT/wheels" \
  --output "$CASTOR_REPLAY_ROOT/input" \
  > "$CASTOR_REPLAY_ROOT/source-preparation.log" 2>&1
python3 - <<'PY'
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from trusted_slot.safe_git import git_environment

root = Path(os.environ["CASTOR_REPLAY_ROOT"])
source = root / "input/target"
candidate = root / "candidate"
evidence = Path("experiments/t372/evidence/live-r2")
expected = json.loads((evidence / "candidate-proof.json").read_text())
manifest = json.loads((root / "input/source-manifest.json").read_text())
patch = (evidence / "actual.patch").resolve()
patch_hash = hashlib.sha256(patch.read_bytes()).hexdigest()
if patch_hash != expected["patch_sha256"] or manifest["target_tree"] != expected["source_tree"]:
    raise SystemExit("recorded patch or original input tree does not match frozen evidence")
shutil.copytree(source, candidate)
subprocess.run(
    ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "apply", str(patch)],
    cwd=candidate, env=git_environment(), check=True, timeout=30,
)

def inventory(path):
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(path).parts
    }

before, after = inventory(source), inventory(candidate)
changed = sorted(k for k in before if before[k] != after.get(k))
if before.keys() != after.keys() or changed != ["boltons/mathutils.py"]:
    raise SystemExit("candidate changed files outside the recorded source patch")
if after["boltons/mathutils.py"] != expected["candidate_source_sha256"]:
    raise SystemExit("candidate source differs from the recorded r2 candidate")
(root / "candidate-proof.json").write_text(json.dumps({
    "source_tree": manifest["target_tree"], "patch_sha256": patch_hash,
    "changed_files": changed, "protected_files_unchanged": True,
    "candidate_source_sha256": after["boltons/mathutils.py"],
    "original_file_sha256": before, "candidate_file_sha256": after,
}, indent=2) + "\n")
PY

# This ignored physical test invokes only native IsolatedVerifier, twice:
# original baseline, then the actual recorded candidate (not the reference fix).
T372_ORACLE_BASELINE="$CASTOR_REPLAY_ROOT/input/target" \
T372_ORACLE_REFERENCE="$CASTOR_REPLAY_ROOT/candidate" \
T372_ORACLE_STATE="$CASTOR_REPLAY_ROOT/oracle" \
CARGO_TARGET_DIR="$CASTOR_REPLAY_ROOT/build" \
cargo test --locked --manifest-path kernel/Cargo.toml \
  --test t372_original_mathutils_oracle -- \
  --ignored --exact original_bits_regression_is_red_then_green_inside_isolated_verifier --nocapture \
  > "$CASTOR_REPLAY_ROOT/oracle.log" 2>&1

docker ps -a --no-trunc --format '{{.ID}}' > "$CASTOR_REPLAY_ROOT/containers-after.txt"
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["CASTOR_REPLAY_ROOT"])
result = json.loads((root / "oracle/result.json").read_text())
image_id = json.loads((root / "image-before.json").read_text())[0]["Id"]
red, green = result["baseline"], result["reference"]
assert red["reason"] == "TEST_VERIFICATION_FAILED" and red["code"] == 1
assert green["reason"] == "NONE" and green["code"] == 0
for value in (red, green):
    assert value["evidence"]["container_removed"] is True
    assert value["evidence"]["inspected_profile"]["image_id"] == image_id
assert "1 failed, 11 passed" in (root / "oracle/red/verifier_stdout.log").read_text()
assert "12 passed" in (root / "oracle/green/verifier_stdout.log").read_text()
before = set((root / "containers-before.txt").read_text().splitlines())
after = set((root / "containers-after.txt").read_text().splitlines())
summary = {
    "status": "VERIFIED", "provider_calls": 0,
    "baseline": "1 failed, 11 passed", "actual_candidate": "12 passed",
    "image_id": image_id, "verifier_containers_removed": True,
    "container_inventory_unchanged": before == after,
}
(root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
PY
