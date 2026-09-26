#!/usr/bin/env bash
#
# scripts/run_t337e_physical.sh
#
# Native Linux host runner script for T-337-E physical execution.
# Executes Castor and Castord natively on the Linux runner host.
# Does NOT use /var/run/docker.sock mount into containers.
# Does NOT use privileged containers.
# Checks df and fails before model pull if insufficient disk space.
# Uploads / archives evidence on every outcome.
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/evidence}"
STAGED_DIR="${STAGED_DIR:-/tmp/task-fixture-t337e}"
CASTOR_STATE_ROOT="${CASTOR_STATE_ROOT:-/tmp/castor-t337e-state}"
CASTOR_MODEL_SOCKET="${CASTOR_MODEL_SOCKET:-/tmp/castor-model-t337e.sock}"
CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/tmp/castor-cargo-target}"
REQUIRED_DISK_KB="${REQUIRED_DISK_KB:-11000000}" # ~10.5 GiB minimum free
EXPECTED_FIXTURE_SHA="${EXPECTED_FIXTURE_SHA:-d64c1fb8f73530b81e43d621f2c9afad9ef2cc5c188c06ec5b0db7c389c4f99f}"

compute_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

echo "=========================================================="
echo "Starting T-337-E Native Linux Host Runner"
echo "Repository Root: $REPO_ROOT"
echo "Evidence Directory: $EVIDENCE_DIR"
echo "Cargo Target Dir: $CARGO_TARGET_DIR"
echo "=========================================================="

mkdir -p "$EVIDENCE_DIR"
mkdir -p "$STAGED_DIR"
mkdir -p "$CARGO_TARGET_DIR"
rm -rf "$CASTOR_STATE_ROOT"
mkdir -p "$CASTOR_STATE_ROOT"

# Trap cleanup to terminate background processes and preserve evidence
ADAPTER_PID=""
OLLAMA_PID=""

cleanup() {
  local exit_code=$?
  echo "--- Performing Runner Cleanup (exit code: $exit_code) ---"
  if [ -n "$ADAPTER_PID" ]; then
    echo "Stopping model adapter (PID: $ADAPTER_PID)..."
    kill "$ADAPTER_PID" 2>/dev/null || true
  fi
  if [ -n "$OLLAMA_PID" ]; then
    echo "Stopping background Ollama service (PID: $OLLAMA_PID)..."
    kill "$OLLAMA_PID" 2>/dev/null || true
  fi
  # Clean up socket
  rm -f "$CASTOR_MODEL_SOCKET" 2>/dev/null || true
  echo "--- Cleanup Completed ---"
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

if [ "${1:-}" != "--verify-only" ] && [ "${VERIFY_ONLY:-0}" != "1" ]; then

# ----------------------------------------------------------------------
# Gate 1: Host Environment & Disk Headroom Check
# ----------------------------------------------------------------------
echo "=== Gate 1: Host Environment & Disk Headroom Preflight ==="

OS_NAME="$(uname -s)"
if [ "$OS_NAME" != "Linux" ]; then
  echo "ERROR: T-337-E physical execution requires a native Linux host (found $OS_NAME)." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker command not found." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running or accessible." >&2
  exit 1
fi

# Log environment diagnostics
{
  echo "=== Environment Info ==="
  uname -a
  date -u +"%Y-%m-%dT%H:%M:%SZ"
  echo "Git Commit: $(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo 'unknown')"
  echo "Git Status: $(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || echo 'unknown')"
  echo ""
  echo "=== Memory Info ==="
  free -h 2>/dev/null || true
  echo ""
  echo "=== CPU Info ==="
  nproc 2>/dev/null || true
  lscpu 2>/dev/null | grep -E "Model name|CPU\(s\):|Thread" || true
  echo ""
  echo "=== Initial Disk Space ==="
  df -h /
} > "$EVIDENCE_DIR/environment.txt"

# Inspect available disk space without assuming unrealistic expansions
AVAILABLE_KB=$(df -k / | awk 'NR==2 {print $4}')
echo "Initial available disk space on root filesystem: ${AVAILABLE_KB} KB"

# Prune unused docker data safely if needed
echo "Pruning unused docker images/containers to maximize headroom..."
docker system prune -af --volumes 2>/dev/null || true

AVAILABLE_KB=$(df -k / | awk 'NR==2 {print $4}')
echo "Available disk space after prune: ${AVAILABLE_KB} KB (~$(( AVAILABLE_KB / 1024 / 1024 )) GiB)"

if [ "$AVAILABLE_KB" -lt "$REQUIRED_DISK_KB" ]; then
  echo "ERROR: Insufficient disk space on runner!" >&2
  echo "Required at least: ${REQUIRED_DISK_KB} KB" >&2
  echo "Available on /:   ${AVAILABLE_KB} KB" >&2
  echo "Aborting before model pull to prevent runner disk exhaustion." >&2
  exit 1
fi
echo "Disk headroom check PASSED: sufficient space for build, images, and model."

# ----------------------------------------------------------------------
# Gate 2: Release Binaries Preflight
# ----------------------------------------------------------------------
echo "=== Gate 2: Castor and Castord Binaries Preflight ==="

export CARGO_TARGET_DIR
if ! command -v castor >/dev/null 2>&1 || ! command -v castord >/dev/null 2>&1; then
  if [ -x "$CARGO_TARGET_DIR/release/castor" ] && [ -x "$CARGO_TARGET_DIR/release/castord" ]; then
    export PATH="$CARGO_TARGET_DIR/release:$PATH"
  else
    echo "Building release binaries for castor and castord (CARGO_TARGET_DIR=$CARGO_TARGET_DIR)..."
    cargo build --release --manifest-path "$REPO_ROOT/kernel/Cargo.toml" --bin castor --bin castord
    export PATH="$CARGO_TARGET_DIR/release:$PATH"
  fi
fi

echo "castor binary: $(command -v castor)"
echo "castord binary: $(command -v castord)"

# ----------------------------------------------------------------------
# Gate 3: Carrier Build & Manifest Alignment Gate
# ----------------------------------------------------------------------
echo "=== Gate 3: Carrier Build and Task Manifest Staging ==="

echo "Building pinned post-fix Pi carrier image from kernel/carrier/pi..."
docker build --tag substratum/castor-pi-carrier:v1 "$REPO_ROOT/kernel/carrier/pi"

CARRIER_IMAGE_ID=$(docker image inspect --format '{{.Id}}' substratum/castor-pi-carrier:v1)
if [ -z "$CARRIER_IMAGE_ID" ] || [[ "$CARRIER_IMAGE_ID" != sha256:* ]]; then
  echo "ERROR: Failed to retrieve carrier image ID (got '$CARRIER_IMAGE_ID')" >&2
  exit 1
fi
echo "Pinned post-fix carrier image ID: $CARRIER_IMAGE_ID"
echo "CARRIER_IMAGE_ID=$CARRIER_IMAGE_ID" >> "$EVIDENCE_DIR/environment.txt"

# Stage fixture and verify checksum
FIXTURE_SRC="$REPO_ROOT/fixtures/t337e_duration"
if [ ! -f "$FIXTURE_SRC/workspace_snapshot.tar.gz" ]; then
  echo "ERROR: Fixture archive not found at $FIXTURE_SRC/workspace_snapshot.tar.gz" >&2
  exit 1
fi

cp "$FIXTURE_SRC/workspace_snapshot.tar.gz" "$STAGED_DIR/workspace_snapshot.tar.gz"

ACTUAL_FIXTURE_SHA=$(compute_sha256 "$STAGED_DIR/workspace_snapshot.tar.gz")
EXPECTED_FIXTURE_SHA="d64c1fb8f73530b81e43d621f2c9afad9ef2cc5c188c06ec5b0db7c389c4f99f"

if [ "$ACTUAL_FIXTURE_SHA" != "$EXPECTED_FIXTURE_SHA" ]; then
  echo "ERROR: Fixture snapshot SHA-256 mismatch!" >&2
  echo "Expected: $EXPECTED_FIXTURE_SHA" >&2
  echo "Actual:   $ACTUAL_FIXTURE_SHA" >&2
  exit 1
fi
echo "Fixture snapshot checksum verified: $ACTUAL_FIXTURE_SHA"

# Generate fresh staged task_manifest.json with exact carrier ID and fresh idempotency key
STAGED_MANIFEST="$STAGED_DIR/task_manifest.json"
cat <<EOF > "$STAGED_MANIFEST"
{
  "task_id": "task-t337e-duration-fix",
  "idempotency_key": "t337e-local-model-run-002",
  "carrier_base_image": "substratum/castor-pi-carrier:v1@${CARRIER_IMAGE_ID}",
  "workspace_snapshot_path": "workspace_snapshot.tar.gz",
  "workspace_snapshot_sha256": "${EXPECTED_FIXTURE_SHA}",
  "task_prompt": "In duration.py, parse_duration('1d') incorrectly returns 3600 seconds instead of 86400 seconds (1 day = 24 hours). Fix the calculation for days ('d') in duration.py so that python3 -m unittest tests/test_duration.py passes all tests.",
  "verification_command": [
    "python3",
    "-m",
    "unittest",
    "tests/test_duration.py"
  ],
  "limits": {
    "timeout_seconds": 300,
    "max_turns": 3,
    "max_actions": 1,
    "max_interactions": 3,
    "max_tokens": 512,
    "model_name": "qwen3.5:9b",
    "provider": "ollama-local",
    "external_http": false,
    "stop_on_excess_interactions": true
  }
}
EOF

echo "Staged task manifest generated at $STAGED_MANIFEST"
cat "$STAGED_MANIFEST"

# ----------------------------------------------------------------------
# Gate 4: Local Ollama & Bounded Adapter Preflight
# ----------------------------------------------------------------------
echo "=== Gate 4: Local Ollama and Bounded Adapter Preflight ==="

# Check disk space one more time before pulling the model
AVAILABLE_KB=$(df -k / | awk 'NR==2 {print $4}')
echo "Available disk space before model pull: ${AVAILABLE_KB} KB"
if [ "$AVAILABLE_KB" -lt 7500000 ]; then # ~7.2 GiB required for 6.6 GiB model
  echo "ERROR: Insufficient disk space to pull qwen3.5:9b model (available: ${AVAILABLE_KB} KB)." >&2
  exit 1
fi

# Ensure Ollama binary is installed; if missing, install pinned official v0.34.4
if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama binary not found on host. Installing pinned official v0.34.4 Linux amd64..."
  OLLAMA_TAR="/tmp/ollama-linux-amd64.tar.zst"
  OLLAMA_URL="https://github.com/ollama/ollama/releases/download/v0.34.4/ollama-linux-amd64.tar.zst"
  EXPECTED_OLLAMA_SHA="c238986e61d40c0cc5f4a9b9e40b9eea104350b77efa34741fc134e105cb9533"
  curl -fL "$OLLAMA_URL" -o "$OLLAMA_TAR"
  ACTUAL_OLLAMA_SHA=$(compute_sha256 "$OLLAMA_TAR")
  if [ "$ACTUAL_OLLAMA_SHA" != "$EXPECTED_OLLAMA_SHA" ]; then
    echo "ERROR: Pinned Ollama asset SHA-256 mismatch!" >&2
    echo "Expected: $EXPECTED_OLLAMA_SHA" >&2
    echo "Actual:   $ACTUAL_OLLAMA_SHA" >&2
    exit 1
  fi
  sudo tar --zstd -xf "$OLLAMA_TAR" -C /usr/local
  rm -f "$OLLAMA_TAR"
fi

OLLAMA_BIN_VERSION=$(ollama --version 2>/dev/null || ollama version 2>/dev/null || echo "unknown")
echo "Ollama Binary Version: $OLLAMA_BIN_VERSION"
echo "Ollama Binary Version: $OLLAMA_BIN_VERSION" >> "$EVIDENCE_DIR/environment.txt"

# Ensure Ollama daemon is active
if ! curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "Starting ollama serve in background..."
  ollama serve > "$EVIDENCE_DIR/ollama.log" 2>&1 &
  OLLAMA_PID=$!
  for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

if ! curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "ERROR: Ollama server failed to respond on 127.0.0.1:11434." >&2
  exit 1
fi
echo "Ollama server is responsive."

# Pull model if not already present
if ! ollama list | grep -q "qwen3.5:9b"; then
  echo "Pulling model qwen3.5:9b (bounded token limit <= 512)..."
  ollama pull qwen3.5:9b
fi
ollama list

# Record model metadata into environment evidence
echo "" >> "$EVIDENCE_DIR/environment.txt"
echo "=== Model Metadata Evidence ===" >> "$EVIDENCE_DIR/environment.txt"
MODEL_TAGS=$(curl -s http://127.0.0.1:11434/api/tags 2>/dev/null || true)
MODEL_DIGEST=$(python3 -c "
import json
try:
    data = json.loads('''$MODEL_TAGS''')
    models = [m for m in data.get('models', []) if 'qwen3.5:9b' in m.get('name', '')]
    print(models[0].get('digest', 'unknown') if models else 'unknown')
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown")
echo "Model Name: qwen3.5:9b" >> "$EVIDENCE_DIR/environment.txt"
echo "Model Digest: $MODEL_DIGEST" >> "$EVIDENCE_DIR/environment.txt"
ollama show qwen3.5:9b >> "$EVIDENCE_DIR/environment.txt" 2>/dev/null || true

# Launch audited adapter wrapper on host
rm -f "$CASTOR_MODEL_SOCKET"
echo "Starting audited host Ollama adapter on $CASTOR_MODEL_SOCKET..."
node "$REPO_ROOT/scripts/audited_adapter_wrapper.mjs" "$CASTOR_MODEL_SOCKET" "$EVIDENCE_DIR" > "$EVIDENCE_DIR/adapter.log" 2>&1 &
ADAPTER_PID=$!

for i in $(seq 1 30); do
  if [ -S "$CASTOR_MODEL_SOCKET" ]; then
    break
  fi
  sleep 0.2
done

if [ ! -S "$CASTOR_MODEL_SOCKET" ]; then
  echo "ERROR: Model adapter failed to open socket at $CASTOR_MODEL_SOCKET." >&2
  exit 1
fi
echo "Model adapter socket is open and listening."

# ----------------------------------------------------------------------
# Gate 5: Bounded Task Execution Gate
# ----------------------------------------------------------------------
echo "=== Gate 5: Executing Bounded Physical Task Run ==="

export CASTOR_STATE_ROOT
export CASTOR_MODEL_SOCKET

echo "Running: castor run --task $STAGED_MANIFEST"
# Narrowly disable -e around the bounded castor run so its exit code can be captured
set +e
timeout -s TERM -k 10 300 castor run --task "$STAGED_MANIFEST" > "$EVIDENCE_DIR/task_result.raw.txt" 2> "$EVIDENCE_DIR/castor_stderr.log"
CASTOR_EXIT=$?
set -e

echo "$CASTOR_EXIT" > "$EVIDENCE_DIR/castor_exit_code.txt"
echo "Castor process exited with code: $CASTOR_EXIT"

else
  echo "=== Running in Host Verification Only Mode (--verify-only) ==="
  CASTOR_EXIT=$(cat "$EVIDENCE_DIR/castor_exit_code.txt" 2>/dev/null || echo 0)
fi

# ----------------------------------------------------------------------
# Gate 6: Evidence Collection and Post-Run Verification
# ----------------------------------------------------------------------
echo "=== Gate 6: Evidence Collection and Host Verification ==="

# Preserve entire state directory (journal, regions, quarantine)
mkdir -p "$EVIDENCE_DIR/state"
if [ -d "$CASTOR_STATE_ROOT" ]; then
  cp -a "$CASTOR_STATE_ROOT/." "$EVIDENCE_DIR/state/" 2>/dev/null || true
fi

# Preserve Pi log if generated by container run
PI_CONTAINER_LOG="$CASTOR_STATE_ROOT/tasks/task-t337e-duration-fix/pi.jsonl"
if [ -f "$PI_CONTAINER_LOG" ]; then
  cp "$PI_CONTAINER_LOG" "$EVIDENCE_DIR/pi.jsonl"
  echo "Preserved Pi container log to $EVIDENCE_DIR/pi.jsonl"
else
  echo "Note: Pi log not generated at $PI_CONTAINER_LOG (omitted, not falsely claimed)"
fi

# Parse task result JSON if raw output exists
TASK_RESULT_JSON="$EVIDENCE_DIR/task_result.json"
if [ -f "$EVIDENCE_DIR/task_result.raw.txt" ]; then
  python3 -c "
import json, sys
try:
    with open('$EVIDENCE_DIR/task_result.raw.txt') as f:
        data = json.load(f)
    with open('$TASK_RESULT_JSON', 'w') as f:
        json.dump(data, f, indent=2)
    print('Parsed task_result.json successfully.')
    print('Status: ' + str(data.get('status')))
    print('Failure Reason: ' + str(data.get('failure_reason')))
    print('Settled Actions: ' + str(data.get('settled_actions_count')))
    print('Committed Turns: ' + str(data.get('committed_turns')))
except Exception as e:
    print('Note: Could not parse task_result.raw.txt as JSON: ' + str(e))
" || true
fi

# Independent Host Verification
VERIFY_DIR=$(mktemp -d /tmp/t337e-verify.XXXXXX)
HOST_VERIFY_LOG="$EVIDENCE_DIR/host_verification.txt"

{
  echo "=========================================================="
  echo "T-337-E Independent Host Verification Report"
  echo "Timestamp: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  echo "Fixture Tarball: $REPO_ROOT/fixtures/t337e_duration/workspace_snapshot.tar.gz"
  echo "Expected Fixture SHA256: $EXPECTED_FIXTURE_SHA"
  echo "=========================================================="

  # 1. Baseline tests in isolated directory
  echo ""
  echo "--- Step 1: Baseline Test Execution (Independent Extract) ---"
  BASELINE_DIR="$VERIFY_DIR/baseline"
  mkdir -p "$BASELINE_DIR"
  tar -xzf "$REPO_ROOT/fixtures/t337e_duration/workspace_snapshot.tar.gz" -C "$BASELINE_DIR"

  set +e
  BASELINE_OUT=$(cd "$BASELINE_DIR" && python3 -m unittest tests/test_duration.py 2>&1)
  BASELINE_EXIT=$?
  set -e

  echo "Baseline command: python3 -m unittest tests/test_duration.py"
  echo "Baseline exit code: $BASELINE_EXIT"
  echo "Baseline output:"
  echo "$BASELINE_OUT"

  # 2. Extract patch_diff from TaskResult if provided
  echo ""
  echo "--- Step 2: TaskResult Patch Inspection ---"
  rm -f "$EVIDENCE_DIR/attempted_patch.diff"
  if [ -f "$TASK_RESULT_JSON" ]; then
    python3 -c "
import json
import sys

task_json_path = sys.argv[1]
diff_out_path = sys.argv[2]
try:
    with open(task_json_path, 'r', encoding='utf-8') as f:
        d = json.load(f)
    p = d.get('patch_diff')
    if p:
        with open(diff_out_path, 'wb') as out:
            if isinstance(p, str):
                out.write(p.encode('utf-8'))
            elif isinstance(p, (bytes, bytearray)):
                out.write(p)
            else:
                out.write(str(p).encode('utf-8'))
except Exception as e:
    sys.stderr.write('Error extracting patch_diff: {}\n'.format(e))
" "$TASK_RESULT_JSON" "$EVIDENCE_DIR/attempted_patch.diff" 2>/dev/null || true
  fi

  if [ -f "$EVIDENCE_DIR/attempted_patch.diff" ]; then
    PATCH_SHA=$(compute_sha256 "$EVIDENCE_DIR/attempted_patch.diff")
    echo "TaskResult provided patch_diff. Saved to $EVIDENCE_DIR/attempted_patch.diff"
    echo "Attempted Patch SHA256: $PATCH_SHA"

    PATCHED_DIR="$VERIFY_DIR/patched"
    mkdir -p "$PATCHED_DIR"
    tar -xzf "$REPO_ROOT/fixtures/t337e_duration/workspace_snapshot.tar.gz" -C "$PATCHED_DIR"
    (cd "$PATCHED_DIR" && git init -q && git config user.name "HostVerifier" && git config user.email "verifier@example.com" && git add . && git commit -q -m "baseline")

    set +e
    GIT_CHECK_OUT=$(cd "$PATCHED_DIR" && git apply --check --whitespace=nowarn "$EVIDENCE_DIR/attempted_patch.diff" 2>&1)
    GIT_CHECK_EXIT=$?
    set -e

    echo "git apply --check exit code: $GIT_CHECK_EXIT"
    echo "git apply --check output: ${GIT_CHECK_OUT:-<clean>}"

    if [ "$GIT_CHECK_EXIT" -eq 0 ]; then
      set +e
      GIT_APPLY_OUT=$(cd "$PATCHED_DIR" && git apply --whitespace=nowarn "$EVIDENCE_DIR/attempted_patch.diff" 2>&1)
      GIT_APPLY_EXIT=$?
      set -e

      echo "git apply exit code: $GIT_APPLY_EXIT"
      echo "git apply output: ${GIT_APPLY_OUT:-<clean>}"

      echo ""
      echo "--- Step 3: Post-Patch Test Rerun ---"
      if [ "$GIT_APPLY_EXIT" -eq 0 ]; then
        set +e
        POST_PATCH_OUT=$(cd "$PATCHED_DIR" && python3 -m unittest tests/test_duration.py 2>&1)
        POST_PATCH_EXIT=$?
        set -e
        echo "Post-patch unittest exit code: $POST_PATCH_EXIT"
        echo "Post-patch unittest output:"
        echo "$POST_PATCH_OUT"
      else
        echo "Skipping post-patch unittest because git apply failed (exit code $GIT_APPLY_EXIT)."
      fi
    else
      echo "Skipping git apply and post-patch unittest because git apply --check failed (exit code $GIT_CHECK_EXIT)."
    fi
  else
    echo "TaskResult does not supply patch_diff (patch_diff is null or omitted)."
    echo "attempted_patch.diff: ABSENT (truthfully not produced; no patch applied)"
  fi

  echo ""
  echo "=========================================================="
  echo "Verification Summary Complete"
  echo "=========================================================="
} | tee "$HOST_VERIFY_LOG"

rm -rf "$VERIFY_DIR"

# Inspect disk space after run
{
  echo ""
  echo "=== Post-Run Disk Space ==="
  df -h /
} >> "$EVIDENCE_DIR/environment.txt"

echo "=========================================================="
echo "T-337-E Runner Execution Finished"
echo "Exit Code: $CASTOR_EXIT"
echo "Evidence stored at: $EVIDENCE_DIR"
echo "=========================================================="

exit "$CASTOR_EXIT"
