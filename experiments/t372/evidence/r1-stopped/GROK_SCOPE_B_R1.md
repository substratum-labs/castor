**REVISE** (Scope B only: live gate, inputs, protocol, and the shown lifecycle diff). This is not whole-task acceptance. The model bridge stays in Scope A.

Controller `--network none`, same-path Engine `/run` scratch, durable state, and exact CID cleanup are unchanged in the diff. Parent `580a9c2d12755d472e534ca3e277e8f7e3ada49e` and fix `d0e36b4db07e5062b35ca664e0516ae0e4890b8b` match the public commits. Spec hash `5286af03a4c75140822e642d39ff6e17c50348f44bd92cb550aa3b2b141022f3` matches `experiments/t372/task-spec.json`. Supplied results stand as given: six existing physical tests pass; explicit-env oracle was red `1` fail / `11` pass and green `12` pass; Pi+MockModel was `12` pass with zero live calls. The old host `pytest.raises` shim is out of scope.

## Critical

**1. Host git runs repository and global config before any safe pack check.**

In the ollama branch of `parse_args`, `git -C <project> rev-parse HEAD^{tree}` and `git status --porcelain` run as soon as the protocol and release JSON parse, before the pin predicate, before the absolute-path / comma / newline checks, and before native Castor pack validation.

Those processes inherit the caller environment and normal git config discovery: system, global, and repo `.git/config`, plus `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*`, `GIT_DIR`, `GIT_WORK_TREE`, and a worktree `.git` gitfile. `alias.rev-parse` or `alias.status` starting with `!` is a shell command. `git status` runs `core.fsmonitor` and configured external diff / textconv commands. `prepare_source.py` uses the same unsanitized `git` for clone, fetch, worktree checkout, `add`, and `commit` (hooks included; no `--no-verify`).

`GIT_DIR` (or a gitfile) also splits identity from the directory that will be mounted: only `boltons/mathutils.py` and `tests/test_mathutils.py` are hashed in Python. A clean foreign git dir can supply the expected tree id and an empty porcelain status while `--project` carries a different verifier, wheels, or other tree.

**2. The oracle pin in the protocol is false, and the gate does not check it.**

`experiments/t372/proposed-protocol.json` has:

`reference_source_sha256` = `0aeac3312b66b3919ec93f35e9785c62f3d79bf6db82d60f3d30ae5` (55 hex chars).

The verified reference digest is:

`0aeac3312b66b3919ec93f35e9785c62f3d79bf6db82d60f3b5a3140b3d30ae5` (64 hex chars).

The live predicate never length-checks or recomputes that field from the reference `boltons/mathutils.py`. `prepare_source.py` records whatever `FIX` checks out and never requires this digest. A release whose `protocol_sha256` matches this file would seal the wrong oracle id. `release.protocol_sha256` binds the lie; it does not detect it.

## Important

**3. Several protocol pins are never closed against what the slot runs.**

`parse_args` does not read `controller_image`, `pi_image`, `verifier_image`, or `wheel_sha256`. `--trusted-controller-image` is still compared only to the inherited `CONTROLLER` constant, not to `sha256:39fad3ec792c6d55d6049f010520ebdc662c92c9eb8f6b74872b7842e073ba80`. Preflight does `write_json(..., validate_model_pin())` and continues. The shown caller never requires that returned pin to equal protocol `model_digest` `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7` or `ollama_version` `0.34.1`.

**4. Call and token caps stop at the parser; the exchange is not integrated into cleanup or the watchdog.**

`HostExchange` is constructed as `(evidence, max(0.001, work_deadline - now))`. The gated `max_unique_http_calls == 3` and `max_output_tokens_per_call == 512` are not passed in. The diff adds `poll()` on the same loop as `discover` / `inspect`, with the timeout captured once at `start()`, and adds no `close` / join on `finish` or on a `start()` failure. A blocking `poll` stalls CID watchdog work. `poll` raising before the deadline uses the existing handler, which sets `TIMEOUT` only when `time.monotonic() >= work_deadline`.

**5. Historical binary provenance is byte identity only.**

Retained pins `castor` `f2217c98e2c92b697b940d69f42a6244197c6fd40d58186ba30a7bb42f09a273` and `castord` `b4051d05982623feb5cfd6e1c1c428f55aab4c7131f2813c7e35f9febb919d04` match the protocol and are what the gate hashes. Kernel/carrier source equal to approved T365 main `364169b` with an empty diff does not show those bytes were built from that commit. No signature, builder id, or reproducible-build record is in scope. The gate must not be described as build attestation.

## Tests

1. Repo-local `.git/config` and a global config each set `core.fsmonitor` and `alias.status` / `alias.rev-parse` to a shell command that creates a marker. Run the ollama parser against an otherwise valid project. Marker files stay absent, including when the protocol mismatch would later `parser.error`. Assert the same for `prepare_source.py` clone / worktree / commit with `core.hooksPath` and fsmonitor set.
2. Export `GIT_DIR` at a clean repo whose `HEAD^{tree}` is `c20df619d8514b7b5c55af93bb3da7eab9251735`, and point `--project` at a tree whose `mathutils.py` and `test_mathutils.py` hashes match the protocol but whose `tests/t372_verify_original_mathutils.sh` or a wheel differs. The gate exits non-zero and does not start the slot.
3. Gate rejects this protocol until `reference_source_sha256` is the 64-char digest above, and it recomputes that digest from the reference file. A 55-char value fails even if `release.protocol_sha256` is recomputed to match.
4. Recompute `release.protocol_sha256` after changing only `controller_image`, `pi_image`, `verifier_image`, or one wheel hash. The gate still rejects. Monkeypatch `validate_model_pin` to return a different model digest or Ollama version; preflight raises before container create.
5. `HostExchange` is built with the gated caps 3 and 512. A fourth HTTP attempt and a 513-token output fail inside the 300s / 60s budget. `poll` is sliced so `discover` / `inspect` still run while a call is outstanding. A `poll` exception at t0+1s yields a non-`SUCCEEDED` launcher status. `finish` after a `start()` failure has closed the exchange.
6. Keep the existing six physical tests, network-none, mock `bits_fixture`, and exact CID cleanup green. Do not treat a hash match of the retained Linux binaries as a provenance test.
