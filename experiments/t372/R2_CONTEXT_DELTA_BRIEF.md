# T-372 additional r2 context trial — focused release review

Yong approved exactly one additional trial with the previously proposed task
context on2026-09-29. This is not an automatic retry. Failed r1 stays immutable.

Task:task-t372-bits-r2. New state:/private/tmp/t372-bits-live-r2 (absent).
Spec SHA256:cb6834c5a88d99c3c236b12e00510d6f0160eae1f33c086ae1e77ba24618ff73.
Protocol SHA256:fe447a4c88f250f2b1e2c1b006cf1b89776e45febe0a285a056a935bb9bb2bc9.

Agent context now names boltons/mathutils.py and tests/test_mathutils.py and
clarifies relative regular-file reads plus one source-only terminal edit. It
contains no repaired code. No listing/search/shell tool was added. A success
would validate this contextualized task, not a generic discovery feature.

Production delta since accepted r1 runtime: launcher LIVE_TASK_ID now binds r2;
file_bridge_fixture rejects either r1 or r2 in both task/idempotency fields.
All trusted_slot modules, kernel/carrier, images/native binaries, model pin,
provider transport, budgeting and cleanup remain unchanged. Refreeze recomputes
all10 runtime hashes and new spec/manifest digest; fresh input tree remains
c20df619d8514b7b5c55af93bb3da7eab9251735. r1 budget digest is preserved in r2
protocol; no old ledger is reset, old input/state/evidence remains unchanged.

Selected limits remain qwen3.5:9b/digest6488c96.../Ollama0.34.1,
think=false/temp0.2/seed17/ctx32768,<=3 additional POSTs,<=512 output each,
300s workload/60s cleanup, no HTTP retry or task rerun. Existing native UDS
transport retries can repeat a denied interaction without new inference.

Witnessed RED: fixture incorrectly accepted newly registered r2 identity in two
positions. GREEN:24 management/recovery tests and4 input tests passed; r2 live
gate positive fixture passes, nine changed pins fail even with rehashed receipts,
retired r1 fails even with matching rewritten receipts. Lint/format/diff clean.

Prior actual physical R2 A/B reviews at8e3596e ACCEPTED the full bridge/gate.
Prior actual r1 real trial:3POSTs/2824input/145output, wrong-path reads, FAILED,
no patch/commit/effect, CLEAN. Actual outcome review ACCEPTED honest accounting
and explicitly did not authorize another trial. New manual approval now exists.

Review the exact delta and frozen r2 binding. ACCEPT permits root to write a
new receipt/release matching this protocol and launch exactly once. No model
POST or release exists yet. After launch, archive every result and independently
verify an actual candidate against original untouched tests inside IsolatedVerifier.
Any failure ends this new trial; no third trial is implied.
