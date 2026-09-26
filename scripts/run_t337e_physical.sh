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

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/evidence}"
STAGED_DIR="${STAGED_DIR:-/tmp/task-fixture-t337e}"
CASTOR_STATE_ROOT="${CASTOR_STATE_ROOT:-/tmp/castor-t337e-state}"
CASTOR_MODEL_SOCKET="${CASTOR_MODEL_SOCKET:-/tmp/castor-model-t337e.sock}"
REQUIRED_DISK_KB="${REQUIRED_DISK_KB:-11000000}" # ~10.5 GiB minimum free

echo "=========================================================="
echo "Starting T-337-E Native Linux Host Runner"
echo "Repository Root: $REPO_ROOT"
echo "Evidence Directory: $EVIDENCE_DIR"
echo "=========================================================="

mkdir -p "$EVIDENCE_DIR"
mkdir -p "$STAGED_DIR"
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

if ! command -v castor >/dev/null 2>&1 || ! command -v castord >/dev/null 2>&1; then
  if [ -x "$REPO_ROOT/kernel/target/release/castor" ] && [ -x "$REPO_ROOT/kernel/target/release/castord" ]; then
    export PATH="$REPO_ROOT/kernel/target/release:$PATH"
  else
    echo "Building release binaries for castor and castord..."
    cargo build --release --manifest-path "$REPO_ROOT/kernel/Cargo.toml" --bin castor --bin castord
    export PATH="$REPO_ROOT/kernel/target/release:$PATH"
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

ACTUAL_FIXTURE_SHA=$(sha256sum "$STAGED_DIR/workspace_snapshot.tar.gz" | awk '{print $1}')
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

# Launch bounded audited adapter on host
rm -f "$CASTOR_MODEL_SOCKET"
echo "Starting bounded host Ollama adapter on $CASTOR_MODEL_SOCKET..."
node "$REPO_ROOT/kernel/carrier/pi/host/ollama_model_adapter.mjs" "$CASTOR_MODEL_SOCKET" > "$EVIDENCE_DIR/adapter.log" 2>&1 &
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
set +e
timeout -s TERM -k 10 300 castor run --task "$STAGED_MANIFEST" > "$EVIDENCE_DIR/task_result.raw.txt" 2> "$EVIDENCE_DIR/castor_stderr.log"
CASTOR_EXIT=$?
set -e

echo "$CASTOR_EXIT" > "$EVIDENCE_DIR/castor_exit_code.txt"
echo "Castor process exited with code: $CASTOR_EXIT"

# ----------------------------------------------------------------------
# Gate 6: Evidence Collection and Post-Run Verification
# ----------------------------------------------------------------------
echo "=== Gate 6: Evidence Collection and Host Verification ==="

# Preserve state directory
mkdir -p "$EVIDENCE_DIR/state"
if [ -d "$CASTOR_STATE_ROOT" ]; then
  cp -a "$CASTOR_STATE_ROOT/." "$EVIDENCE_DIR/state/" 2>/dev/null || true
fi

# Parse task result JSON if valid
python3 -c "
import json, sys
try:
    with open('$EVIDENCE_DIR/task_result.raw.txt') as f:
        data = json.load(f)
    with open('$EVIDENCE_DIR/task_result.json', 'w') as f:
        json.dump(data, f, indent=2)
    print('Parsed task_result.json successfully.')
    print('Status: ' + str(data.get('status')))
    print('Failure Reason: ' + str(data.get('failure_reason')))
    print('Settled Actions: ' + str(data.get('settled_actions_count')))
    print('Committed Turns: ' + str(data.get('committed_turns')))
    if data.get('patch_diff'):
        with open('$EVIDENCE_DIR/applied.diff', 'w') as f:
            f.write(data['patch_diff'])
except Exception as e:
    print('Note: Could not parse task_result.raw.txt as JSON: ' + str(e))
" || true

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
