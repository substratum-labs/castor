ACCEPT

Scope B matches the bounded single-demo contract. No Critical or Important blocker. This does not release a live attempt.

The ollama gate in `parse_args` refuses to continue unless CLI identity, timeouts, and the raw protocol hash all match the frozen trial: task `task-t372-bits-r1`, 300s/60s, model `qwen3.5:9b`, digest equal to `MODEL_DIGEST`, Ollama `0.34.1`, `think=false`, temperature `0.2`, seed `17`, context `32768`, and caps `3` and `512`. The same raw hash must appear on the acceptance receipt (`accepted=true`, scopes exactly `["model_bridge", "live_gate"]` in that order) and on the release. Re-hashing an edited protocol into those receipts still fails, because image, wheel, runtime, native, reference, and cap fields are compared to values recomputed from disk and to the runtime-pinned constants. `CONTROLLER`, `PI`, `VERIFIER`, `MODEL_DIGEST`, and `REFERENCE_SHA256` live in files covered by `runtime_source_sha256`, so changing a constant changes a pinned source hash.

`clean_tree` is raw plumbing, not status porcelain: `/usr/bin/git`, a replacement environment (inherited `GIT_DIR` / config is not passed), `core.fsmonitor=false`, `core.hooksPath=/dev/null`, and `HEAD^{tree}` plus `ls-tree` / `ls-files`. The index must equal HEAD, every worktree byte and mode must match, and extras or missing tracked files fail closed. The reference SHA-256 is recomputed from another directory and must equal both `0aeac3312b66b3919ec93f35e9785c62f3d79bf6db82d60f3b5a3140b3d30ae5` and the protocol. The reference root cannot be the target or sit inside it. Target `source` / `tests` / `spec` hashes and tree `c20df619d8514b7b5c55af93bb3da7eab9251735` are bound separately, so the repair file cannot stand in for the pre-fix input.

`HostExchange` is stored before `start()` and `finish()` closes it on the way out, including when `start()` raises. The workload loop uses `check()`, inspect, and `sleep(0.15)`, not a blocking poll. The preflight checks the controller and Pi pins, checks the verifier id against the protocol in ollama mode, and requires ELF magic. The fixture mode cannot use the live task id. The stub evidence is a different identity (`task-t372-bits-bridge-pinned`, two stub POSTs, zero provider calls, `CLEAN`) and is not live-gate evidence.

Nonblocking limitations, including preserved trust limits:

- Retained `castor` / `castord` pins are exact ELF byte hashes plus a magic check. They are not a cryptographic build attestation. `binary_provenance` already says so.
- Model binding is operator-local tag, version, and `response` metadata. It does not identify weights.
- The receipt is a root-written review record. It is not a signature against a malicious root.
- `live_state_root`, `source_manifest_sha256`, and `input_preparation_sha256` are not separate gate fields. State binds as release versus CLI; manifest reference, tree, source, tests, and wheels are recomputed; preparation is bound through the resulting tree and the spec/source/test hashes.
- Only `run_trusted_slot.py` and `trusted_slot/*.py` are runtime-pinned. The controller mount is the whole `trusted_slot` directory.
- The parent cap is cooperative. Bridge close runs before pause/SIGKILL. Pin checks and later mounts are not atomic. Repo-local Git config is still read; hooks and fsmonitor are overridden.
- The unit test stubs `RUNTIME_FILES` and `REFERENCE_SHA256`. The production text does contain that reference constant. Ollama-mode has not been executed; the 23 passing bridge tests and 4 passing safe-Git tests do not launch it.
