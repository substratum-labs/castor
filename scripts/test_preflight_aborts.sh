#!/usr/bin/env bash
#
# scripts/test_preflight_aborts.sh
#
# Offline test suite and static assertions for preflight aborts and safety invariants.
# Strictly runs offline without launching Ollama or live model inference.
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SCRIPT="$REPO_ROOT/scripts/run_t337e_physical.sh"
WORKFLOW_FILE="$REPO_ROOT/.github/workflows/t337e-physical-gate.yml"

echo "=== Running Static Assertions for Preflight Invariants ==="

# 1. Assert set -euo pipefail in run script
if ! grep -q "^set -euo pipefail" "$RUN_SCRIPT"; then
  echo "FAIL: $RUN_SCRIPT must begin with 'set -euo pipefail'" >&2
  exit 1
fi
echo "PASS: Script declares set -euo pipefail"

# 2. Assert narrow disabling of -e around castor run
if ! grep -B 3 "timeout -s TERM -k 10 300 castor run" "$RUN_SCRIPT" | grep -q "set +e"; then
  echo "FAIL: Expected 'set +e' immediately before bounded castor run" >&2
  exit 1
fi
if ! grep -A 4 "timeout -s TERM -k 10 300 castor run" "$RUN_SCRIPT" | grep -q "set -e"; then
  echo "FAIL: Expected 'set -e' immediately after bounded castor run" >&2
  exit 1
fi
echo "PASS: -e is narrowly disabled only around bounded castor run"

# 3. Assert CARGO_TARGET_DIR is isolated under temp
if ! grep -q "CARGO_TARGET_DIR=.*tmp" "$RUN_SCRIPT"; then
  echo "FAIL: $RUN_SCRIPT must isolate CARGO_TARGET_DIR under temp" >&2
  exit 1
fi
echo "PASS: CARGO_TARGET_DIR isolated under temp"

# 4. Assert pinned Ollama asset checksum in script and workflow
EXPECTED_SHA="c238986e61d40c0cc5f4a9b9e40b9eea104350b77efa34741fc134e105cb9533"
if ! grep -q "$EXPECTED_SHA" "$RUN_SCRIPT"; then
  echo "FAIL: $RUN_SCRIPT missing pinned Ollama SHA-256 $EXPECTED_SHA" >&2
  exit 1
fi
if ! grep -q "$EXPECTED_SHA" "$WORKFLOW_FILE"; then
  echo "FAIL: $WORKFLOW_FILE missing pinned Ollama SHA-256 $EXPECTED_SHA" >&2
  exit 1
fi
echo "PASS: Pinned Ollama SHA-256 present in both runner script and workflow"

# 5. Assert workflow does NOT ignore label removal failure
if grep -q "remove-label.*|| true" "$WORKFLOW_FILE"; then
  echo "FAIL: $WORKFLOW_FILE must not ignore label removal failures with '|| true'" >&2
  exit 1
fi
echo "PASS: Label removal does not use '|| true'"

# 6. Assert audited adapter wrapper is invoked
if ! grep -q "audited_adapter_wrapper.mjs" "$RUN_SCRIPT"; then
  echo "FAIL: $RUN_SCRIPT must invoke audited_adapter_wrapper.mjs" >&2
  exit 1
fi
echo "PASS: Audited adapter wrapper is invoked"

# 7. Assert Python block in run script imports sys
if ! grep -A 5 "Step 2: TaskResult Patch Inspection" "$RUN_SCRIPT" | grep -q "import sys"; then
  echo "FAIL: $RUN_SCRIPT must import sys in Step 2 patch inspection" >&2
  exit 1
fi
echo "PASS: Step 2 patch inspection imports sys"

# 8. Assert exact patch bytes are written directly from Python without shell echo
if grep -q 'echo "\$PATCH_DIFF"' "$RUN_SCRIPT"; then
  echo "FAIL: $RUN_SCRIPT must not use shell 'echo \"\$PATCH_DIFF\"' to write attempted_patch.diff" >&2
  exit 1
fi
if ! grep -q "with open(diff_out_path, 'wb')" "$RUN_SCRIPT"; then
  echo "FAIL: $RUN_SCRIPT must write exact patch bytes directly from Python" >&2
  exit 1
fi
echo "PASS: Exact patch bytes written directly from Python to evidence file"

# 9. Assert git apply --check runs before git apply
if ! grep -B 2 -A 5 "git apply --check" "$RUN_SCRIPT" | grep -q "git apply --check"; then
  echo "FAIL: $RUN_SCRIPT must execute git apply --check" >&2
  exit 1
fi
echo "PASS: git apply --check is executed before git apply"

echo ""
echo "=== Running Dynamic Offline Preflight Abort Tests ==="

TMP_TEST_DIR=$(mktemp -d /tmp/castor-abort-test.XXXXXX)
trap 'rm -rf "$TMP_TEST_DIR"' EXIT

# Dynamic Test 1: OS check aborts on non-Linux
set +e
OS_OUT=$(
  MOCK_BIN="$TMP_TEST_DIR/bin"
  mkdir -p "$MOCK_BIN"
  cat <<'MOCK' > "$MOCK_BIN/uname"
#!/bin/sh
if [ "$1" = "-s" ]; then echo "FreeBSD"; else echo "FreeBSD test-host"; fi
MOCK
  chmod +x "$MOCK_BIN/uname"
  PATH="$MOCK_BIN:$PATH" EVIDENCE_DIR="$TMP_TEST_DIR/ev1" bash "$RUN_SCRIPT" 2>&1
)
OS_EXIT=$?
set -e

if [ "$OS_EXIT" -eq 0 ]; then
  echo "FAIL: Expected non-Linux OS to abort, but exited 0" >&2
  exit 1
fi
if ! echo "$OS_OUT" | grep -q "requires a native Linux host"; then
  echo "FAIL: Non-Linux abort output did not contain expected message. Got: $OS_OUT" >&2
  exit 1
fi
echo "PASS: Non-Linux host aborts before any build or model actions (exit code $OS_EXIT)"

# Dynamic Test 2: Insufficient disk space aborts before build/model
set +e
DISK_OUT=$(
  MOCK_BIN="$TMP_TEST_DIR/bin2"
  mkdir -p "$MOCK_BIN"
  cat <<'MOCK' > "$MOCK_BIN/uname"
#!/bin/sh
if [ "$1" = "-s" ]; then echo "Linux"; else echo "Linux test-host"; fi
MOCK
  cat <<'MOCK' > "$MOCK_BIN/docker"
#!/bin/sh
if [ "$1" = "info" ]; then exit 0; fi
exit 0
MOCK
  chmod +x "$MOCK_BIN/uname" "$MOCK_BIN/docker"
  PATH="$MOCK_BIN:$PATH" REQUIRED_DISK_KB="999999999999" EVIDENCE_DIR="$TMP_TEST_DIR/ev2" bash "$RUN_SCRIPT" 2>&1
)
DISK_EXIT=$?
set -e

if [ "$DISK_EXIT" -eq 0 ]; then
  echo "FAIL: Expected insufficient disk space to abort, but exited 0" >&2
  exit 1
fi
if ! echo "$DISK_OUT" | grep -q "Insufficient disk space on runner"; then
  echo "FAIL: Disk headroom abort output did not contain expected error. Got: $DISK_OUT" >&2
  exit 1
fi
echo "PASS: Insufficient disk space aborts preflight cleanly (exit code $DISK_EXIT)"

# Dynamic Test 3: Fixture SHA mismatch aborts
set +e
FIXTURE_OUT=$(
  MOCK_BIN="$TMP_TEST_DIR/bin3"
  mkdir -p "$MOCK_BIN"
  cat <<'MOCK' > "$MOCK_BIN/uname"
#!/bin/sh
if [ "$1" = "-s" ]; then echo "Linux"; else echo "Linux test-host"; fi
MOCK
  cat <<'MOCK' > "$MOCK_BIN/docker"
#!/bin/sh
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "system" ]; then exit 0; fi
if [ "$1" = "build" ]; then exit 0; fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then echo "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"; exit 0; fi
exit 0
MOCK
  cat <<'MOCK' > "$MOCK_BIN/cargo"
#!/bin/sh
exit 0
MOCK
  cat <<'MOCK' > "$MOCK_BIN/castor"
#!/bin/sh
exit 0
MOCK
  cat <<'MOCK' > "$MOCK_BIN/castord"
#!/bin/sh
exit 0
MOCK
  cat <<'MOCK' > "$MOCK_BIN/sha256sum"
#!/bin/sh
echo "corrupt_sha_00000000000000000000000000000000000000000000000000000000  $1"
MOCK
  cat <<'MOCK' > "$MOCK_BIN/shasum"
#!/bin/sh
echo "corrupt_sha_00000000000000000000000000000000000000000000000000000000  $1"
MOCK
  chmod +x "$MOCK_BIN"/*
  PATH="$MOCK_BIN:$PATH" REQUIRED_DISK_KB="1" EVIDENCE_DIR="$TMP_TEST_DIR/ev3" bash "$RUN_SCRIPT" 2>&1
)
FIXTURE_EXIT=$?
set -e

if [ "$FIXTURE_EXIT" -eq 0 ]; then
  echo "FAIL: Expected fixture checksum mismatch to abort, but exited 0" >&2
  exit 1
fi
if ! echo "$FIXTURE_OUT" | grep -q "Fixture snapshot SHA-256 mismatch"; then
  echo "FAIL: Checksum mismatch output did not contain expected error. Got: $FIXTURE_OUT" >&2
  exit 1
fi
echo "PASS: Fixture snapshot SHA-256 mismatch aborts before model pull (exit code $FIXTURE_EXIT)"

echo ""
echo "=== Running Offline Host Verification Regression Tests ==="
bash "$REPO_ROOT/scripts/test_host_verification.sh"

echo ""
echo "=== ALL OFFLINE PREFLIGHT ABORT TESTS AND STATIC ASSERTIONS PASSED! ==="
