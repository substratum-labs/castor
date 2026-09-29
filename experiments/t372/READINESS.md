# Current: T-372 merged; fresh zero-model replay repaired by T-377

The archived real r2 success stands. The [current replay command](DEMO_WALKTHROUGH.md)
rebuilds its actual patch with baseline 1 failed/11 passed and candidate 12 passed;
both verifiers removed, zero provider calls. See [repair evidence](REPLAY_REPAIR.md).
The records below retain their historical status and are not current gates.

---

# Current: r2 real-model repair succeeded, accepted physical outcome audit; Yong review pending

See [r2 report](LIVE_TRIAL_R2_REPORT.md). Two actual calls, 12 protected and
12 independent original tests pass, cleanup CLEAN. Path context supplied; generic
discovery remains unimplemented. Both releases consumed; no new inference.

---

Historical checkpoints below; their status applies only at their recorded time.

# T-372 real trial r1 failed — no repair delivered

2026-09-29: both physical R2 scopes ACCEPTED the bridge/gate. The one approved
real Qwen trial ran and FAILED / MODEL_INTERACTION_ERROR. It made3 actual POSTs,
2824 input tokens and145 output tokens; all calls completed but each requested a
wrong file/directory path. No source was read and no patch/turn/effect committed.
The fourth unique interaction was denied by budget; cleanup CLEAN.

See [live failure report](LIVE_TRIAL_R1_REPORT.md) and
[evidence/live-r1](evidence/live-r1/SHA256.json). Independent protected replay of
the unchanged authoritative input still has1fail/11pass. No second trial ran.
The [path-context-only proposal](proposed-task-spec-r2.json) is not released.
T-372 remains CHANGES_REQUESTED pending a new trial decision. Old T-365 outcomes
are unchanged. The immutable protocol's status describes its pre-release freeze;
actual review/release/outcome records provide the later state.

---

Historical preparation checkpoints and failed records follow:

# T-372 packaging repaired; new bridge preflight passed

2026-09-28: Yong reopened the stopped preparation. The clone128 blocker is fixed
and the actual Pi/FileBridge/loopback-HTTP/protected-verifier path now succeeds.
This is a deterministic, zero-model preflight: **not a live model result**.

See [repair report](CLONE_REPAIR.md) and [bounded evidence](evidence/clone-repair/).
Both the cached partial repository and fresh public HTTPS source reproduce the
frozen target tree. Offline controller pack succeeds; protected pytest reports
12 passed; task SUCCEEDED; 2 stub POSTs; 0 provider calls; cleanup CLEAN.
23 bridge/recovery tests and 4 Git/preparation tests pass, with lint/format clean.
Read-only audit verified 14 journal CRCs, 8 Region hashes and their references.
Physical Grok accepted the narrow packaging repair after withdrawing a disproved
ref-tip blocker. This does not accept the broader pending bridge/gate R2.
CI at code commit842320a passed all7 jobs; later evidence-only commits are distinct.

Remaining gates: finish/freeze current input and runtime protocol hashes,
independent R2 bridge/gate acceptance, then release and audit the already
budget-approved real trial. No acceptance receipt or live release is supplied.
The earlier stop and R1 reviews below remain historical evidence.

---

# T-372 stopped preparation — demo not delivered

Status: CHANGES_REQUESTED / stopped before live release. Yong accepted one local Qwen3.5:9b trial (<=3 calls, <=512 output tokens/call, 300s, no retries), but no live inference has occurred.

The original protected pytest oracle and deterministic old-MockModel Pi fixture succeeded as recorded below. Physical Grok R1 returned REVISE for both model bridge and gate; those are archived in evidence/r1-stopped. The partial R1 correction is not independently accepted and must not be merged or released.

Current corrected-source checks: 23 management/exchange unit tests and 2 safe-Git tests passed. Budget reset, response completion/model identity, unsupported content, bounded UDS error response and process deadline each have witnessed failing tests followed by passing tests. Prior source 0af4350 had CI7/7; that is NOT a CI claim for this changed source.

Three zero-model preparation attempts did not establish the new full bridge: (1) HTTP stub startup blocked on reverse DNS; (2) the controller could not pack a Git worktree whose gitdir was outside its /project mount; (3) standalone clone returned128, so the dependent pack check had no input directory. Third failure triggers the project stop rule. No registered live attempt, provider POST, or trial success is claimed. The R2 controller/HTTP worker and stub were removed; cleanup reported CLEAN. Failed raw records remain in scoped host evidence and bounded excerpts under evidence/r1-stopped.

Remaining work after human reset: capture exact clone stderr and fix the self-contained input producer; run the actual Pi/FileBridge/HTTP-stub/protected-verifier preflight; finish input/runtime protocol hashes and independent R2 acceptance; then release exactly the authorized live trial and independently audit it. The live gate requires a separate acceptance receipt; no accepted receipt or released live token is supplied.

Verified reviewer qualifications: Git aliases do not replace builtins; git status is unnecessary for our raw HEAD/index/filesystem comparison. The shared JSON writer already uses atomic rename and file/directory fsync. The bridge state mount is available only to the trusted controller, not Pi/verifier. Original first-system section mapping follows the existing Pi adapter; unsupported block types now fail rather than disappear. Reused native binaries are byte-pinned and core/carrier source unchanged; this is not cryptographic build attestation. A provider response has no weight digest field: real mode checks tags/version before and after and response model name/done, assuming the operator-controlled local provider is not adversarially retagged mid-call.

---

Historical initial preparation packet (superseded status, evidence retained):

# T-372 preparation status for independent review

**Status: source implementation and zero-model preflight ready for review; live
attempt unreleased.** No Ollama `/api/chat` or `/api/generate` request was sent.
No task result in this packet is a real-model task result.

The exact proposed live input is the clean Boltons Git tree
`c20df619d8514b7b5c55af93bb3da7eab9251735`, from original pre-fix parent
`580a9c2d12755d472e534ca3e277e8f7e3ada49e` with original fix-commit
regression tests, offline pytest wheels, and the protected verification command.
`boltons/mathutils.py` SHA-256 is
`3d81dc8b44136299857373a8f53d50dbf0be7aabfa34e7bcfee7c6b41b3b3236`.
The public reference repair source SHA-256 is
`0aeac3312b66b3919ec93f35e9785c62f3d79bf6db82d60f3b5a3140b3d30ae5`
and remains outside the live target. `prepare_source.py` reproduced the same
target tree from the cached public Git source without executing target code.

Accepted zero-model checks:

1. Real Rust `IsolatedVerifier` plus genuine offline pytest on all 12 original
   test functions: pre-fix exit 1 `TEST_VERIFICATION_FAILED`, reference exit 0
   `NONE`; both protected containers removed. Raw structured evidence SHA-256:
   `1d58f2e43eca6f2d60813d55c0bc7863cf974d695fe4714e81df6dfa4988b12d`.
2. Separate deterministic Bits fixture through native Pi read, edit, Core
   settlement, and protected verifier: `12 passed`, two mock interactions,
   zero provider calls, cleanup `CLEAN`. Result SHA-256:
   `85d49a5f89a8ff320976c1482fa25679b49ccb896d4781e4d7e5b11559e46280`.
   The raw journal remains at the durable local state path specified in README.
3. Management bridge and gate unit suite: 17 tests passed. Existing T-371
   physical mock suite: 6 tests passed in 288.765 seconds. Post-suite Docker
   inventory contained only the pre-existing `epic_agnesi` container. Mock
   fixing, nonfixing, and timeout paths were retained.
4. Read-only Ollama `/api/tags` and `/api/version` returned the proposed
   `qwen3.5:9b` digest
   `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`
   and version `0.34.1`; this was not an inference call.

Pending gates: Yong's model/budget selection, independent physical technical
review, root registration of the single live attempt, and a root-authored
release JSON matching the frozen protocol SHA-256
`19163ebcde590d09e297673fc7a3b42b4fbd57065705311c33c7d5b81a088093`.
The launcher will reject absent or mismatched release/input/runtime pins before
HTTP. If Yong selects different model or budget, revise protocol and repeat
review before release. Do not self-release, retry a live task, or merge this PR.

Known limits and deviations:

- The first probe used a substitute `pytest.raises` runner and ran
  Boltons on the host. That probe is excluded from accepted evidence; the
  diagnostic Rust result is preserved under `evidence/excluded/`. The runner
  was removed, and the corrected accepted oracle uses genuine pytest only in
  `IsolatedVerifier`.
- The reused Linux `castor`/`castord` binary hashes are pinned, and current
  kernel/carrier source matches `main`; available build metadata does not
  cryptographically prove the historical binaries came from that exact source
  tree. Reviewer should decide whether this provenance is sufficient before
  release or request a fresh pinned build. No T-365 build volume was modified.
- The bridge's actual Ollama POST path is unit-tested with local fake HTTP and
  the native request envelopes captured in the mock preflight; it has not been
  exercised against a live model. Missing usage and transport errors are
  incomplete measurements and consume their reserved call. The deterministic
  fixture is a positive control, not evidence of model repair ability.
