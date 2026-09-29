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
