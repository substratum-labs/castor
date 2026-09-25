# Castor Pi One-Shot Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) for tracking.

**Goal:** Make `castor run --task manifest.json` run one governed Pi coding task and return a truthful, durable result without Python in the product runtime.

**Architecture:** A Rust `castor` CLI supervises packaging, Pi/Roche lifecycle, host model I/O, trusted workspace effects, independent tests, and terminal result projection. The existing Rust `castord` remains the sole C-01/C-03/C-06 authority writer; Pi receives only the agent UDS socket. The accepted T-337-C test branch is the RED baseline, and implementation advances through narrow GREEN gates.

**Tech Stack:** Rust 2021, `castor-kernel`, Roche Docker profile, AISA framed UDS, Node.js 22.19+, `@earendil-works/pi-coding-agent@0.87.1`, OCI images. Python is permitted only in historical/external tests, never the shipped task path.

**Spec:** `../substratum-internal/design/castor/rfc/t337b_one_shot_task_and_pi_integration_rfc.md` Revision 4, plus `../substratum-internal/design/castor/architecture/next/l4/c03_interaction_contract.md` and Castor PR #12 at `3bc404e`.

## Global Constraints

- The task package is a regular archive selected by `workspace_snapshot_path` relative to the manifest directory; reject absolute paths, `..`, symlinks, missing files, and archive traversal before extraction.
- Hash raw archive bytes against `workspace_snapshot_sha256` before unpacking or Docker build. Launch only a recorded `sha256:` image digest, never a mutable tag.
- Roche has exactly one read-only `/run/castor/ipc.sock` mount, no network, read-only rootfs, UID/GID 10001, and no ambient provider credentials.
- Model output is fully buffered, stored as an immutable C-01 Region, and bound through a trusted host path before the agent consumes it. No token stream is claimed.
- Action payloads bind to committed Turns before trusted actuator retrieval; `UNKNOWN_DISPUTED` cannot become `SUCCEEDED` without evidence reconciliation and host verification.
- Production task/host/carrier/extension paths must run without Python. Node.js >= 22.19.0 and Pi 0.87.1 are pinned.
- `--allow-test-opcodes` and every `CASTOR_TEST_*` seam are inert in production mode.

## Review Focus

- A valid archive replaced after hashing must not change the bytes used to build the image: stage and build from the same opened, verified bytes.
- An archive entry containing `..`, absolute paths, symlinks, devices, or hard links must not escape or mutate the private build context.
- A repeated idempotency key with a conflicting manifest digest must fail instead of reusing another task's result.
- A Pi process exit 0 without a settled action or a passing independent test must remain `FAILED`.
- An agent-issued `ReportOutcome` must never bind model bytes; only the trusted host channel may bind them.

## File Map

- `kernel/src/bin/castord.rs`: narrow gateway role enforcement; no task policy or model credentials.
- `kernel/src/bin/castor.rs`: public `run --task` argument parsing and JSON result output.
- `kernel/src/one_shot/{manifest,image,model,actuator,supervisor,result}.rs`: bounded task orchestration split by responsibility.
- `kernel/src/lib.rs`, `kernel/Cargo.toml`: expose the new Rust module/binary and only required archive/build dependencies.
- `kernel/carrier/pi/{Dockerfile,package.json,package-lock.json,castor-pi-extension.js}`: reproducible, Python-free Pi carrier and AISA extension.
- `kernel/tests/one_shot_cli_contract.rs`, `kernel/tests/host_aisa_gateway_contract.rs`: existing RED acceptance suite; refine only if a fixture is physically invalid, preserving the normative assertion.
- `.github/workflows/ci.yml` (or the existing kernel job file): build the carrier and run physical gates on Linux before claiming release readiness.

## Task 1: Close the C-03 reporter authority gap

**Files:** Modify `kernel/src/bin/castord.rs`. Test `kernel/tests/host_aisa_gateway_contract.rs`.

**Interfaces:** Agent `RequestInteraction`/`ConsumeInteraction` remain accepted. `ReportOutcome` and `ReportInteractionOutcome` belong to trusted host control (or a dedicated authenticated host channel). Historical fixture compatibility is gated by `--allow-test-opcodes` only.

The relevant role-gate edit has this exact shape:

```rust
let test_opcode_allowed = allow_test_opcodes
    && matches!(request.op.as_str(), "ReportOutcome" | "ReportInteractionOutcome" | "Replay" | "__ProviderSubmissionCount" | "__LoseAdapterDedupState");
let control_report_allowed = matches!(request.op.as_str(), "ReportOutcome" | "ReportInteractionOutcome");
```

- [ ] Run `cargo test --manifest-path kernel/Cargo.toml --test host_aisa_gateway_contract agent_channel_cannot_bind_its_own_model_observation -- --exact`; confirm current `Ok` versus expected `Error` RED.
- [ ] Remove the two reporter opcodes from the unconditional `agent_allowed` match; add them to `control_allowed`. Retain temporary agent reporter access only under `allow_test_opcodes` so existing test-only fixtures can migrate.
- [ ] Run the exact test, then the full `host_aisa_gateway_contract` suite with UDS access; require 26/26 green and no other role expansion.
- [ ] Commit `fix: restrict C03 outcome reports to trusted host channel`.

## Task 2: Implement manifest and truthful terminal result parsing

**Files:** Create `kernel/src/one_shot/manifest.rs`, `result.rs`, `mod.rs`, `kernel/src/bin/castor.rs`; modify `kernel/src/lib.rs`, `kernel/Cargo.toml`.

**Interfaces:** `TaskManifest::load(path: &Path) -> Result<ValidatedManifest, TaskFailure>` opens only a regular archive beneath the manifest directory; `TaskResult` serializes the RFC's common terminal fields and success-only fields. `castor run --task PATH` emits one JSON result on stdout and diagnostics on stderr.

The accepted manifest fields are concrete:

```rust
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskManifest {
    task_id: String,
    idempotency_key: String,
    carrier_base_image: String,
    workspace_snapshot_path: std::path::PathBuf,
    workspace_snapshot_sha256: String,
    task_prompt: String,
    verification_command: Vec<String>,
}
```

- [ ] Run the existing preflight tests in `one_shot_cli_contract`; confirm RED at absent `CARGO_BIN_EXE_castor`.
- [ ] Add `[[bin]] name = "castor"` and a Rust CLI that parses `run --task PATH`, rejects invalid manifest/archive paths, computes SHA-256 over verified archive bytes, and emits `FAILED (SNAPSHOT_DIGEST_MISMATCH)` without fabricated downstream fields.
- [ ] Use `serde(deny_unknown_fields)` on the manifest, reject empty `verification_command`, and bind idempotency identity to the complete validated manifest digest.
- [ ] Run preflight tests and `cargo clippy --manifest-path kernel/Cargo.toml --tests -- -D warnings`; commit `feat: validate one-shot manifest and emit truthful preflight results`.

## Task 3: Stage immutable snapshot bytes and build a digest-pinned image

**Files:** Create `kernel/src/one_shot/image.rs`, `kernel/carrier/pi/Dockerfile`, `package.json`, `package-lock.json`; modify `kernel/Cargo.toml` only for required archive codecs.

**Interfaces:** `stage_snapshot(&ValidatedManifest, &Path) -> Result<StagedSnapshot, TaskFailure>` copies the already-hashed archive bytes to a private staging root, rejects unsafe archive entries, and extracts only regular files/directories. `build_derived_image(&StagedSnapshot, &str) -> Result<ImageDigest, TaskFailure>` produces a `sha256:` image identifier recorded before Roche starts.

The generated task Dockerfile is built only from the verified private stage:

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY --chown=10001:10001 --chmod=0555 workspace_snapshot/ /workspace/
```

- [ ] Run `image_build_failure_returns_truthful_failed_result` and the two Pi carrier physical gates; confirm RED.
- [ ] Implement a build context containing only the verified snapshot and a generated Dockerfile with `FROM <pinned carrier digest>` and read-only `/workspace` contents. Treat Docker nonzero/invalid digest as `PROVISIONING_IMAGE_BUILD_FAILED` and create no container.
- [ ] Build the carrier with Node.js >=22.19.0 and exactly `@earendil-works/pi-coding-agent@0.87.1`; omit Python packages/interpreter. Verify image ID and launch derived images by that ID.
- [ ] Run the image failure, carrier version/no-Python, and `/workspace` EROFS physical tests, then record build logs and commit `feat: build immutable one-shot Pi task image`.

## Task 4: Add a durable task board, idempotency, and child supervision

**Files:** Create `kernel/src/one_shot/supervisor.rs`; extend `result.rs` and `kernel/src/bin/castor.rs`.

**Interfaces:** `OneShotTaskSupervisor::run(manifest_path: &Path) -> TaskResult` persists task identity, manifest digest, image digest, agent generation, and state transitions under `CASTOR_TEST_TASK_STATE_ROOT` only when test mode is explicit; production uses a configured host state root. Reopening the same idempotency key returns the existing projection without a second child.

Task status has only the RFC transitions, with no success bypass:

```rust
enum TaskStatus {
    Submitted,
    Provisioning,
    Active,
    UnknownDisputed,
    Evaluating,
    Succeeded,
    Failed,
    FencedCancelled,
}
```

- [ ] Run `duplicate_active_submission_reuses_task_without_starting_another_agent`, `agent_crash_before_committed_action_fails_without_effects`, and H2; confirm RED after the CLI gate advances.
- [ ] Start `castord` as the single journal writer, pass only the agent socket to Roche, and use test-only `CASTOR_TEST_AGENT_CHILD`/`CASTOR_TEST_DERIVED_IMAGE_DIGEST` when `--allow-test-opcodes` is present.
- [ ] Persist and fsync task state before acknowledging submission, fence the lease on child crash/timeout, and run host verification independent of Pi JSONL success claims.
- [ ] Run D1/C2/H2 tests, reject conflicting duplicate manifest digests, then commit `feat: supervise durable one-shot tasks`.

## Task 5: Mediate model I/O through trusted C-03 binding

**Files:** Create `kernel/src/one_shot/model.rs`; connect it from `supervisor.rs`; retain role enforcement in `castord.rs`.

**Interfaces:** The guest requests `RequestInteraction` over agent IPC. The host model service uses protected credentials (or the local `CASTOR_TEST_MODEL_SOCKET` only in test mode), buffers a complete response, persists its Region via C-01, reports via trusted control, then permits `ConsumeInteraction`.

The host's reporter envelope carries the existing AISA fields after Region persistence:

```json
{"op":"ReportOutcome","payload":{"interaction_id":"interaction-1","observation_region_id":"region://observation","observation_digest":"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}}
```

- [ ] Run C1 and N1; confirm expected model/error/binding REDs.
- [ ] Implement framed mock-model service support only in test mode; a disconnected model produces `MODEL_INTERACTION_ERROR` with zero effects. In production, bound the provider response size and retry/time budgets without leaking credentials into Roche.
- [ ] Persist the full response before `ReportOutcome`; the first pre-binding Consume must reject, and the later Consume must return only the bound bytes.
- [ ] Run C1/N1, `c03_interaction_contract`, and agent-channel reporter denial; commit `feat: bind buffered model results through trusted host`.

## Task 6: Apply edits through a trusted actuator and verify patches

**Files:** Create `kernel/src/one_shot/actuator.rs`; connect it from `supervisor.rs`.

**Interfaces:** The actuator reads only `AcquireDispatch` for an armed attempt, validates payload digest and workspace target scope, applies a unified diff to private CoW staging, and sends authenticated settlement on `evidence.sock`. The host runs `verification_command` in the staging workspace and hashes its output and final patch.

The untrusted edit payload is data until the trusted actuator verifies its binding:

```json
{"action_type":"WorkspaceEdit","target_path":"defect.txt","patch":"--- a/defect.txt\n+++ b/defect.txt\n@@ -1 +1 @@\n-failing fixture\n+fixed fixture\n"}
```

- [ ] Run N1 and C4; confirm RED at missing settlement/verification path.
- [ ] Reject absolute/parent traversal edit targets and payload hash mismatches; record `AttemptSettled` only after signed evidence. Keep test-only `CASTOR_TEST_ACTUATOR_MODE` below the real authority boundary.
- [ ] For `UNKNOWN_DISPUTED`, probe staging bytes against the bound payload, settle if exact, then transition to `EVALUATING` and execute the host test before any success result.
- [ ] Run N1/C4/H2 and existing actuator/evidence contracts; commit `feat: settle one-shot edits and verify host result`.

## Task 7: Finish fault, stale, and hostile paths

**Files:** Modify `kernel/src/one_shot/supervisor.rs`, `model.rs`, `actuator.rs`; adjust test fixtures only for confirmed invalid setup.

**Interfaces:** Test-only `CASTOR_TEST_FAULT_POINT` supports `crash_post_attempt_armed` and `fence_after_admit_before_commit`; normal mode rejects or ignores all `CASTOR_TEST_*` settings. Recovery reads C-01 and task board facts, never trusts Pi transcript or child exit code as authority.

The dispute recovery guard is explicit:

```rust
match (status, evidence_verified) {
    (TaskStatus::UnknownDisputed, true) => TaskStatus::Evaluating,
    (TaskStatus::UnknownDisputed, false) => TaskStatus::UnknownDisputed,
    (other, _) => other,
}
```

- [ ] Run C3, D1, L1, H1, and all preflight fault tests; confirm their current REDs.
- [ ] On post-arm crash, reopen as `UNKNOWN_DISPUTED` without replay; on fence, reject late `CommitTurn`; on unauthorized agent opcode, fence and produce `SECURITY_VIOLATION`.
- [ ] Run the full one-shot and gateway suites, strict Clippy/rustfmt, and the current non-Docker Rust baseline. Record any remaining red by test name; commit `feat: recover and fence one-shot task faults`.

## Task 8: Integrate real Pi extension and physical carrier checks

**Files:** Complete `kernel/carrier/pi/castor-pi-extension.js`; add focused Node tests under `kernel/carrier/pi/tests/`; update the existing Linux CI job to build the carrier before physical gates.

**Interfaces:** `pi.registerProvider("castor")` implements complete-buffered host model exchange; `pi.registerTool` exposes only read, mediated edit/test, and finish operations. Pi runs `--mode json` once; `agent_settled` is diagnostic and task success remains host-authoritative.

The one-shot carrier launches Pi through its official JSON mode after loading the adapter:

```sh
pi --extension /opt/castor/castor-pi-extension.js --mode json "Repair the failing unit test."
```

- [ ] Verify the pinned package's official ExtensionAPI signatures and disable ambient Pi tools that could bypass mediated effects.
- [ ] Add the smallest extension tests for AISA framing, tool mapping, and no direct network/host write; run them before implementing the extension behavior.
- [ ] Build/run the carrier and derived image under Roche; verify Node/Pi versions, no Python, one UDS mount, no network, and `/workspace` EROFS.
- [ ] Run all 22 one-shot tests and all 26 gateway tests GREEN, existing kernel tests, `cargo clippy --all-targets -- -D warnings`, rustfmt, and Linux CI. Commit, push, and submit T-337-D to agy review without merging to main ahead of T-337-E's product decision.

## Self-Review

- Every RFC §6.1 gate maps to a test in Tasks 1–8. The preflight hash and terminal-result gates have explicit failure and success tests.
- All four external side effects (OCI build, model provider, agent process, actuator) have bounded test seams; only the Rust supervisor and C-01 journal decide task state.
- The plan changes the C-03 reporter channel before enabling Pi, preserving the newly observed security RED gate.
- The `UNKNOWN_DISPUTED` path explicitly passes through host verification and never jumps to `SUCCEEDED`.
