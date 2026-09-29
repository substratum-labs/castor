# T-372 demo walkthrough

The real r2 trial repaired the Boltons Bits negative-index bug in two Qwen calls.
Its actual patch passed 12 original tests and a separate protected replay. T-372
was approved and merged as PR #23. The later zero-model recipe failure and its
T-377 correction are documented in [REPLAY_REPAIR.md](REPLAY_REPAIR.md).

## Reproduce the recorded patch with zero model calls

Prerequisites: Docker Desktop or local Linux Docker, Python 3 with pip,
Rust/Cargo, and the pinned verifier image. Run from a checkout containing T-377:

```sh
# Provision exactly the frozen verifier image if it is not already available.
docker pull docker.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
bash experiments/t372/replay_candidate.sh
```

The script creates a new temporary directory and prints its path, including on
failure. It downloads pinned public source and four hash-checked pytest wheels;
zero-model does not mean network-free preparation. Target code executes only in
native `IsolatedVerifier`, with its networkless, read-only candidate profile.
No agent, Ollama bridge or live release is started.

The command checks the original tree and archived actual patch hash, copies the
baseline into a fresh candidate, and verifies that only `boltons/mathutils.py`
changed. Original tests, verifier command and wheels stay identical. It compiles
the checked-in physical oracle with `cargo --locked` in a fresh build directory
and explicitly runs its otherwise-ignored test against baseline and candidate.
The test's historical `reference` result field means the **actual r2 candidate**
in this recipe, not the separate upstream known repair.

Expected summary: `VERIFIED`, `provider_calls: 0`, baseline `1 failed, 11 passed`,
actual candidate `12 passed`, and `verifier_containers_removed: true`.
`container_inventory_unchanged` separately compares before/after container IDs;
concurrent unrelated Docker work can affect that comparison.

Retained artifacts include `summary.json`, `candidate-proof.json`, source/wheel
preparation logs, `oracle.log`, `oracle/result.json`, and each verifier's captured
stdout/stderr. An unavailable image stops at preflight and preserves its error
in `image-preflight.stderr`; the runner never substitutes another image.

## What the live demo established

1. Input: authentic pre-fix Boltons parent plus original upstream regression tests.
2. Task: repair Bits negative indexing, with exact source and test paths supplied.
3. Native Pi reads both files; real Qwen emits one terminal source edit.
4. Castor commits/settles the edit, verifies it, and returns `SUCCEEDED` with CLEAN teardown.
5. The zero-model command independently checks this recorded patch.

See [TaskResult](evidence/live-r2/task-result.json), [patch](evidence/live-r2/actual.patch),
[original test log](evidence/live-r2/verifier_stdout.log), and [r2 report](LIVE_TRIAL_R2_REPORT.md).
This replay does not repeat model generation, prove generic project discovery,
or establish a repair success rate. Both original live releases remain consumed.
