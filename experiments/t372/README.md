> **STOPPED / NOT LIVE-READY:** Read [READINESS.md](READINESS.md). R1 preparation ended at the three-failure stop rule; no live inference occurred. The old launch examples below describe the initial candidate and are not a released demo. Current live gate additionally requires --source-manifest and --acceptance-file. No acceptance/release is supplied.

# T-372 Bits preparation and review packet

This is **preparation only**. No Ollama chat/generate call or registered live
attempt has run. Yong's model/budget selection, independent technical review,
and a root-authored release file are still required. The source branch is for a
draft PR; do not merge or interpret the deterministic fixture as a live success.

## Authentic task pin

The public [Boltons PR #450](https://github.com/mahmoud/boltons/pull/450)
documents that `Bits('1011')[-1]` returned `False` even though the final bit is
`True`, and too-negative integer indexes also returned `False` rather than
raising. The original fixing commit is
`d0e36b4db07e5062b35ca664e0516ae0e4890b8b`; its **own parent** is
`580a9c2d12755d472e534ca3e277e8f7e3ada49e`. PR merge commit:
`5eb6cc045560abf320712f262346ae95fb66d448`. The original fix changes
only `boltons/mathutils.py` and `tests/test_mathutils.py`. This public history
is available to a model; this is a historical bug demo, not a blind evaluation.

The live input tree is parent source plus the original regression test file and
offline pytest wheels. It contains no repaired `mathutils.py` or reference patch.
The reference workspace is separate. Full source SHA-256: pre-fix
`3d81dc8b44136299857373a8f53d50dbf0be7aabfa34e7bcfee7c6b41b3b3236`,
reference `0aeac3312b66b3919ec93f35e9785c62f3d79bf6db82d60f3b5a3140b3d30ae5`;
original regression file
`9331906c1e944368f677e06731bda4fb114a50d8eb1df992fe87767e644ea94b`.
The upstream license is BSD-3-Clause, `LICENSE` SHA-256
`c301912653a8d8c99eab6212aa3aea8d164ea249d8ad53c941e0558a0a5ac1e3`.
The reproducible target Git tree is `c20df619d8514b7b5c55af93bb3da7eab9251735`;
the local target commit ID is not a portable pin because commit timestamps vary.

`prepare_source.py` creates an exclusive output directory with `upstream/`,
`target/`, `reference/`, and `source-manifest.json`. It checks the fixing parent,
changed paths, and wheel hashes. It only copies source; it never imports or runs
Boltons. Example:

```sh
python3 experiments/t372/prepare_source.py --output /private/tmp/t372-fresh
```

The four read-only cached wheels were copied from
`/private/tmp/t363-boltons-candidate/wheels` into fresh T-372 candidate trees.
Their SHA-256 values match the wheel checksums on the
[pytest 8.3.5](https://pypi.org/project/pytest/8.3.5/),
[iniconfig 2.0.0](https://pypi.org/project/iniconfig/2.0.0/),
[packaging 24.2](https://pypi.org/project/packaging/24.2/), and
[pluggy 1.5.0](https://pypi.org/project/pluggy/1.5.0/) PyPI pages. Exact wheel
hashes are in `proposed-protocol.json`. The verifier command installs them only
inside its networkless `/workspace` candidate, then invokes genuine pytest on
all of the unchanged `tests/test_mathutils.py`.

## Accepted zero-model evidence

The opt-in Rust test `kernel/tests/t372_original_mathutils_oracle.rs` calls the
real `IsolatedVerifier` twice, with the pre-fix target and separate reference.
The [accepted result](evidence/oracle/pytest-red-green.json) records baseline
`TEST_VERIFICATION_FAILED`/exit 1 and reference `NONE`/exit 0. Both verifiers
used image ID `sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`,
networkless read-only profiles, and were removed. The implementation file is
the only difference relevant to the verification command; tests, wheels, and
command are identical. This is the independent target RED/GREEN oracle, with
zero model calls.

Reproduce with fresh state and the prepared source paths:

```sh
T372_ORACLE_BASELINE=/private/tmp/t372-fresh/target \
T372_ORACLE_REFERENCE=/private/tmp/t372-fresh/reference \
T372_ORACLE_STATE=/private/tmp/t372-fresh/oracle-state \
CARGO_TARGET_DIR=/private/tmp/t372-cargo-target \
cargo test --manifest-path kernel/Cargo.toml \
  --test t372_original_mathutils_oracle --offline -- --nocapture
```

The deterministic native Pi fixture used a **separate clone** of the target
tree, task ID `task-t372-bits-preflight-1`, and `--mock-mode bits_fixture`. The
[native result](evidence/mock-preflight/task-result.json),
[mock accounting](evidence/mock-preflight/mock-summary.json),
[Pi log](evidence/mock-preflight/pi.jsonl),
[mock exchanges](evidence/mock-preflight/mock-calls.jsonl), and
[verifier stdout](evidence/mock-preflight/verifier_stdout.log) show a real
`castor_read_file` observation, a terminal `castor_edit_file`, protected pytest
`12 passed`, one settled action, and zero HTTP/provider calls. The
[lifecycle](evidence/mock-preflight/lifecycle.jsonl),
[cleanup](evidence/mock-preflight/cleanup.json), and
[quiescence](evidence/mock-preflight/quiescence.json) record exact-CID teardown.
The raw Core journal remains in durable local state at
`/private/tmp/t372-bits-preflight-state/tasks/task-t372-bits-preflight-1/core-journal.log`
(SHA-256 `a8aa41da63ce99a453121518a465be9a41a01e4b063bd1afa0a5bf4496e3ed8b`).
The baseline/reference protected oracle state is at
`/private/tmp/t372-pytest-oracle-state/result.json`.

Reproduce the Pi fixture with a fresh cloned target and state directory:

```sh
python3 scripts/run_trusted_slot.py \
  --project /private/tmp/t372-fresh/mock-target \
  --task-spec experiments/t372/task-spec.json \
  --task-id task-t372-bits-preflight-1 \
  --idempotency-key task-t372-bits-preflight-1 \
  --state-root /private/tmp/t372-fresh/mock-state \
  --mock-mode bits_fixture --scratch-parent /run \
  --linux-bin-dir /private/tmp/t365-native-build/debug \
  --trusted-controller-image sha256:39fad3ec792c6d55d6049f010520ebdc662c92c9eb8f6b74872b7842e073ba80 \
  --workload-timeout 300 --cleanup-timeout 60
```

Create `mock-target` as a clean local clone of `target` first. Do not use the
live target directory for the positive control. The reused Linux binaries have
SHA-256 pins in the protocol; the current source worktree's kernel and carrier
files match `main`. The binary build's historical source provenance is not
cryptographically proven by the available build metadata, so independent
review should check that limitation before release.

## Proposed live protocol and gate

[`proposed-protocol.json`](proposed-protocol.json) is the frozen proposal for
**one** registered `task-t372-bits-r1` attempt: local Ollama `qwen3.5:9b`
digest `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`,
Ollama 0.34.1, `think:false`, temperature 0.2, seed 17, context 32768,
at most three unique HTTP calls, at most 512 requested/measured output tokens
per call, a 300-second workload, and separate 60-second cleanup. The proposed
protocol SHA-256 is `19163ebcde590d09e297673fc7a3b42b4fbd57065705311c33c7d5b81a088093`.
This is a budget proposal pending Yong's selection, not approval or a released
attempt. If the selected model/budget differs, update and re-review the protocol
and hash before any release.

The live launcher requires `--model-mode ollama`, the exact protocol file, and
a root-authored release JSON outside the source tree. It checks task/idempotency
ID, clean target Git tree, pre-fix source, original tests, spec and native binary
hashes, runtime budgets and model options, release fields, and local Ollama
`/api/tags` plus `/api/version` before starting native work. The release JSON
must contain matching `task_id`, `protocol_sha256`, `model_digest`,
`target_tree`, `spec_sha256`, exact `state_root`, timeouts and call/token caps,
with `budget_approved:true` and `root_released:true`. No such release is supplied
here. The root coordinator owns registration and the one-attempt gate.

For each unique interaction, the trusted host durably writes the reservation
before its single HTTP POST to local `/api/chat`. It stores the full native and
Ollama requests, raw HTTP response bytes, parsed response, available token/time
usage, status, and error. Missing usage is `null`/`INCOMPLETE`, not zero; errors,
timeouts, and malformed or >512-token responses spend the reservation. Retries
with the same ID and bytes return a cached successful observation or fail if
the first request did not complete. Changed same-ID requests and a fourth unique
call are denied. The controller remains networkless and forwards native UDS
frames through the persistent state exchange to the trusted host pump. Pi and
verifier retain their original networkless native profiles; the controller
does not run target code. This adapter follows the existing Pi/Ollama message,
system-section, tool, and tool-result conversion rather than replacing the
agent's prompt.

## Excluded diagnostic deviation

Before coordinator correction, a temporary substitute `pytest.raises` runner
was used and Boltons tests were executed on the host. That was outside the
accepted protocol. Its raw Rust result is preserved at
[`evidence/excluded/shim-oracle-result.json`](evidence/excluded/shim-oracle-result.json)
and its host console output is not part of accepted evidence. The substitute
runner has been removed from the source and target apparatus. Only the later
genuine-pytest protected runs above support the accepted oracle claim.
