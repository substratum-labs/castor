# T-372 real-model trial r2: repair delivered for review

Status: successful bounded demo; final outcome review pending. 2026-09-29.

Yong approved one additional trial after r1 failed on file discovery. The only
agent-context change names `boltons/mathutils.py` and `tests/test_mathutils.py`,
with terminal-edit guidance; it supplies no solution. The launcher binds the new
r2 identity. No directory/search tool, Core or sandbox semantics were changed.
The original r1 result and T-365 evidence remain unchanged.

## Actual result

Native Pi + local qwen3.5:9b returned **SUCCEEDED / NONE** in **42.410848s**.
Two actual HTTP POSTs completed (200/done): **5511 input / 477 output tokens**;
output counts 64 and 413. Approved caps were three POSTs, 512 output per call,
300s workload and 60s cleanup. No automatic trial or HTTP retry occurred.
Call one reads both exact source/test paths; call two submits a terminal edit.

The model changes only `boltons/mathutils.py`: normalize negative indexes by
adding bit length, then reject indexes outside either bound. Actual patch SHA256
`e35f63502842921053786af1b4298f63e77f2348ba3586d79cba05294734820e`.
Native protected original pytest: **12 passed (0.02s)**. Independent reconstruction
from frozen input plus this exact patch preserves tests, wheels and verifier;
the Rust IsolatedVerifier replay separately reports **12 passed (0.01s)**.
The original protected baseline remains 1 failed / 11 passed, reference 12 passed.

Native board equals TaskResult, one committed turn and one settled action.
Read-only audit verified **14 CRC-valid journal frames, 8 Region content hashes
and their referenced digests**. This is not semantic journal replay.
Cleanup **CLEAN**, task and independent verifier containers removed, recorded
launcher process group empty. Container inventory after audit equals the prior
inventory; unrelated `epic_agnesi` was preserved. r1 budget bytes are unchanged.

## Frozen configuration and evidence

Protocol: [frozen-protocol-r2.json](frozen-protocol-r2.json), SHA256
`fe447a4c88f250f2b1e2c1b006cf1b89776e45febe0a285a056a935bb9bb2bc9`.
Model digest `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`;
Ollama 0.34.1; think=false, temperature .2, seed17, num_ctx32768.
State `/private/tmp/t372-bits-live-r2`, task `task-t372-bits-r2`.
Actual physical Grok delta review accepted before registration/release.

[Evidence inventory](evidence/live-r2/SHA256.json) covers native TaskResult,
actual HTTP/native exchanges, budget, journal/Regions, exact patch, protected
logs, independent proof/replay, cleanup and actual process exits.
[Audit](evidence/live-r2/audit.json) records token sums and POST body serialization
matches. [Walkthrough](DEMO_WALKTHROUGH.md) explains offline replay.
Registration and frozen status fields are historical pre-launch checkpoints;
TaskResult and this report describe the later outcome.

## Interpretation and limits

This completes a historical real-repository repair demo with supplied file paths.
It does not establish statistical reliability, generic project discovery or a
blind benchmark: the public upstream fix is available to a model. Across these
separately authorized trials r1 failed, r2 succeeded, total five actual POSTs;
these are not pooled with T-365 or presented as a reliability estimate.

Local inference API fee $0; hardware, energy and reviewer costs unmeasured.
Retained Linux binary hashes identify bytes, not cryptographic build provenance.
The trusted Python launcher remains management/test apparatus; Rust Core is
native authority. This demo does not prove a wholly Python-free distribution.
Both trial releases are consumed; no further live inference is authorized.
