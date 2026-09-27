#!/usr/bin/env bash
#
# scripts/test_host_verification.sh
#
# Focused offline regression suite for host verification in Gate 6 of run_t337e_physical.sh.
# Asserts:
# 1. Valid patch_diff:
#    - Exact patch bytes written directly from Python to attempted_patch.diff (no shell echo/substitution).
#    - Independently runs git apply --check on fresh extracted snapshot.
#    - Independently runs git apply on fresh extracted snapshot.
#    - Runs all 7 tests in tests/test_duration.py and verifies all pass (exit code 0).
# 2. Absent patch_diff:
#    - Correctly handles null / omitted patch_diff.
#    - attempted_patch.diff is truthfully absent and not created.
#    - Baseline tests run and failure recorded truthfully; post-patch apply/test is skipped.
# 3. Invalid patch_diff:
#    - git apply --check detects rejection.
#    - Skips git apply and post-patch unittest.
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SCRIPT="$REPO_ROOT/scripts/run_t337e_physical.sh"

echo "=========================================================="
echo "Running Focused Offline Host Verification Regression Suite"
echo "Repository Root: $REPO_ROOT"
echo "=========================================================="

TMP_TEST_DIR=$(mktemp -d /tmp/castor-verify-test.XXXXXX)
trap 'rm -rf "$TMP_TEST_DIR"' EXIT

# ----------------------------------------------------------------------
# Test 1: Valid Patch Regression
# ----------------------------------------------------------------------
echo ""
echo "=== Test 1: Host Verification with Valid Patch ==="

EV1="$TMP_TEST_DIR/ev1"
mkdir -p "$EV1"

EXPECTED_PATCH_FILE="$TMP_TEST_DIR/expected.patch"
cat <<'EOF' > "$EXPECTED_PATCH_FILE"
--- a/duration.py
+++ b/duration.py
@@ -38,5 +38,5 @@ def parse_duration(spec: str) -> int:
         return value * 3600
     if unit == "d":
         # Defect: incorrectly using 3600 instead of 86400 (24 * 3600)
-        return value * 3600
+        return value * 86400
     raise ValueError(f"Unknown duration unit: {unit}")
EOF

python3 -c "
import json, sys
with open(sys.argv[2], 'r', encoding='utf-8') as pf:
    patch = pf.read()
data = {
    'status': 'SUCCEEDED',
    'failure_reason': 'NONE',
    'settled_actions_count': 1,
    'committed_turns': [1],
    'patch_diff': patch
}
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)
" "$EV1/task_result.json" "$EXPECTED_PATCH_FILE"

EVIDENCE_DIR="$EV1" bash "$RUN_SCRIPT" --verify-only

# Assert attempted_patch.diff exists and matches exact bytes
if [ ! -f "$EV1/attempted_patch.diff" ]; then
  echo "FAIL: Expected $EV1/attempted_patch.diff to exist for valid patch!" >&2
  exit 1
fi

if ! cmp -s "$EV1/attempted_patch.diff" "$EXPECTED_PATCH_FILE"; then
  echo "FAIL: Extracted patch bytes did not match original patch!" >&2
  diff -u "$EXPECTED_PATCH_FILE" "$EV1/attempted_patch.diff" >&2
  exit 1
fi
echo "PASS: Exact patch bytes written directly from Python to evidence file"

# Assert host_verification.txt report contents
HOST_VERIFY_REPORT="$EV1/host_verification.txt"
if [ ! -f "$HOST_VERIFY_REPORT" ]; then
  echo "FAIL: Expected $HOST_VERIFY_REPORT to exist!" >&2
  exit 1
fi

if ! grep -q "Baseline exit code: 1" "$HOST_VERIFY_REPORT"; then
  echo "FAIL: Expected baseline test failure in host verification report" >&2
  exit 1
fi
echo "PASS: Baseline test execution correctly identified unpatched defect"

if ! grep -q "git apply --check exit code: 0" "$HOST_VERIFY_REPORT"; then
  echo "FAIL: Expected 'git apply --check exit code: 0' in verification report" >&2
  exit 1
fi
echo "PASS: Independent git apply --check passed cleanly"

if ! grep -q "git apply exit code: 0" "$HOST_VERIFY_REPORT"; then
  echo "FAIL: Expected 'git apply exit code: 0' in verification report" >&2
  exit 1
fi
echo "PASS: Independent git apply succeeded"

if ! grep -q "Post-patch unittest exit code: 0" "$HOST_VERIFY_REPORT"; then
  echo "FAIL: Expected 'Post-patch unittest exit code: 0' in verification report" >&2
  exit 1
fi

if ! grep -q "Ran 7 tests" "$HOST_VERIFY_REPORT" || ! grep -q "OK" "$HOST_VERIFY_REPORT"; then
  echo "FAIL: Expected all 7 tests to pass in post-patch output" >&2
  cat "$HOST_VERIFY_REPORT" >&2
  exit 1
fi
echo "PASS: All 7 tests executed and passed post-patch"

# ----------------------------------------------------------------------
# Test 2: Absent Patch Regression
# ----------------------------------------------------------------------
echo ""
echo "=== Test 2: Host Verification with Absent Patch ==="

EV2="$TMP_TEST_DIR/ev2"
mkdir -p "$EV2"

python3 -c "
import json, sys
data = {
    'status': 'FAILED',
    'failure_reason': 'MODEL_INTERACTION_ERROR',
    'settled_actions_count': 0,
    'committed_turns': [],
    'patch_diff': None
}
with open(sys.argv[1], 'w') as f:
    json.dump(data, f, indent=2)
" "$EV2/task_result.json"

EVIDENCE_DIR="$EV2" bash "$RUN_SCRIPT" --verify-only

# Assert attempted_patch.diff was NOT created
if [ -f "$EV2/attempted_patch.diff" ]; then
  echo "FAIL: attempted_patch.diff must NOT exist when patch_diff is absent/null!" >&2
  exit 1
fi
echo "PASS: attempted_patch.diff is not created when patch_diff is null"

HOST_VERIFY_REPORT2="$EV2/host_verification.txt"
if ! grep -q "attempted_patch.diff: ABSENT" "$HOST_VERIFY_REPORT2"; then
  echo "FAIL: Verification report missing 'attempted_patch.diff: ABSENT'" >&2
  cat "$HOST_VERIFY_REPORT2" >&2
  exit 1
fi

if grep -q "git apply exit code" "$HOST_VERIFY_REPORT2"; then
  echo "FAIL: git apply should not be executed when patch_diff is absent" >&2
  exit 1
fi
echo "PASS: Truthfully reported absence of patch_diff without executing patch apply"

# ----------------------------------------------------------------------
# Test 3: Invalid Patch Rejection Regression
# ----------------------------------------------------------------------
echo ""
echo "=== Test 3: Host Verification with Invalid Patch (Check Rejection) ==="

EV3="$TMP_TEST_DIR/ev3"
mkdir -p "$EV3"

INVALID_PATCH="--- a/duration.py
+++ b/duration.py
@@ -999,1 +999,1 @@
-nonexistent line that does not exist in duration.py
+replacement line
"

python3 -c "
import json, sys
data = {
    'status': 'FAILED',
    'failure_reason': 'PATCH_VALIDATION_FAILED',
    'settled_actions_count': 1,
    'committed_turns': [1],
    'patch_diff': '''$INVALID_PATCH'''
}
with open(sys.argv[1], 'w') as f:
    json.dump(data, f, indent=2)
" "$EV3/task_result.json"

EVIDENCE_DIR="$EV3" bash "$RUN_SCRIPT" --verify-only

HOST_VERIFY_REPORT3="$EV3/host_verification.txt"
if ! grep -q "Skipping git apply and post-patch unittest because git apply --check failed" "$HOST_VERIFY_REPORT3"; then
  echo "FAIL: Expected git apply to be skipped when git apply --check fails!" >&2
  cat "$HOST_VERIFY_REPORT3" >&2
  exit 1
fi
echo "PASS: git apply --check rejected invalid patch and prevented corrupt git apply / test rerun"

echo ""
echo "=========================================================="
echo "ALL OFFLINE HOST VERIFICATION REGRESSION TESTS PASSED!"
echo "=========================================================="
