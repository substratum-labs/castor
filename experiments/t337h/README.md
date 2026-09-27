# T-337-H: Pi editing-contract diagnostic experiment

Status: REVIEW; experimental evidence, not a product API change or E acceptance.
Date: 2026-09-27 UTC. Product baseline: `6e0139b` (physical Run 36297837163).
Owner: Codex; reviewer: Yong.

## Finding

The current model-facing edit contract contributes avoidable failures. A fresh
controlled pair using the same local pinned Qwen model produced an invalid raw
diff and a valid structured text replacement. The latter compiled to a patch
that passed `git apply --check` and all seven fixture tests. This is a promising
repair path, not proof of general reliability or a full Castor success.

A separate adapter defect is confirmed: Pi 0.87.1 stores system-prompt text in
`sections`; Castor's Ollama translator discards it. The archived first request
renders to 1,611 characters with Pi's own `getSystemMessageText`, but zero through
Castor. Existing `host/test_adapter.mjs` even asserts the empty result at line 758;
that test encoded the bug. The A/B pair deliberately leaves this defect unchanged
in both arms, isolating the edit interface rather than combining fixes.

## Experiment 1: replay the exact historical patch

`inputs/model.patch` is unedited model output extracted from Run 36297837163.
Input hashes and source log hash are in `inputs/provenance.json`.
Each case starts from a fresh extraction of the SHA-pinned archive. The test
runner is diagnostic tooling; it adds no Python requirement to Castor runtime.

| Case | Existing guest lint | Host git check | Fixture tests | Interpretation |
| --- | --- | --- | --- | --- |
| Original | reject: hunk count | reject: corrupt patch | not run | Failure reproduced |
| Add final newline only | reject: hunk count | reject | not run | Newline alone insufficient |
| Change 7/7 to 6/6 only | accept | reject: corrupt patch | not run | Guest lint is incomplete, as expected for non-authoritative lint |
| Correct counts + newline | accept | accept | 7/7 | Model chose a behaviorally sufficient change |
| Same intent via Pi text replacement/diff generator | accept | accept | 7/7 | Identical resulting file hash to preceding case |
| Investigator-written clean text replacement | accept | accept | 7/7 | Cleaner candidate; not model output |

The first two passing cases replace the defect comment with the correct return,
leaving the original return unreachable. Seven green tests do not prove code
quality. The clean investigator candidate removes the old return too.

## Experiment 2: two fresh real local model observations

Execution: `live-probe.mjs`, 2026-09-27 UTC. Inputs are the archived request after
Pi had read `duration.py` (interaction-1-2). The actual existing
`OllamaModelAdapter` performs request digest validation and provider translation.
Only the advertised editing tool description/schema changes in the pair. The
structured arm uses `path, edits[{oldText,newText}]`, matching Pi's native shape,
with exact/unique/non-overlapping semantics. The raw arm uses `path, patch_diff`.

Controls: local `http://127.0.0.1:11434/api/chat`, Qwen `qwen3.5:9b`, digest
`6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`, Q4_K_M;
Ollama 0.34.1 on macOS, Node v26.0.0; temperature 0, seed 337 in both arms.
Maximum 2 calls, <=512 generated tokens each, 120s client deadline per call,
no retries. No returned tool was dispatched. The script refuses an accidental
rerun if its evidence file already exists. No GitHub workflow was triggered.

| Arm | Input/output tokens | Latency | Raw model result | Independent result |
| --- | --- | --- | --- | --- |
| Raw diff | 803 / 256 | 15,938ms | wrong hunk count, missing final newline, changed code incorrectly marked as unchanged context | Rejected; even count/newline repair still cannot apply |
| Text replacement | 874 / 283 | 11,424ms | exact day-branch match, replaces comment and bad return with correct return | strict compiler -> git check -> apply -> 7/7; no dead return |

Total: exactly 2 live HTTP calls, 1,677 input and 539 output tokens.
The fixture test execution uses reviewed generated code in disposable directories.
`live-results.json` records full provider payloads/responses and converted output.
`probe-results.json` records candidate diffs and strict matching results.
`replay-results.json` records git errors, test output, and resulting file hashes.

Limits: one sample per arm; A precedes B and cache warmth differs, so latency is
not a performance finding. Local Ollama 0.34.1 differs from the historical Linux
runner's 0.34.4. This is an after-read interface probe, not a new full Pi/Roche/
Castor run, and does not demonstrate journal admission, settlement or SUCCEEDED.
The system-prompt defect's behavioral effect was not tested with a model.

## Native Pi comparison and boundary

Read the installed 0.87.1 source in carrier image
`sha256:2ab95f51e58acba2c5db9ee089709f9a94b1be91164ae09d6a62fef0ca170e77`:

- `pi-coding-agent/dist/core/tools/edit.js`: model supplies targeted text edits;
  code handles matching and patch generation. Default operations write the file.
- `pi-coding-agent/dist/core/tools/edit-diff.js`: rejects empty/missing/ambiguous/
  overlapping edits, but also has fuzzy matching and newline normalization.
- `pi-ai/dist/utils/text.js`: `getSystemMessageText` joins content and sections;
  `renderSystemMessageUpdate` handles later section updates/removals.

The experiment calls native pure diff helpers only, never native filesystem
write operations. Pi-generated file headers need explicit `a/` and `b/` adaptation
for Castor's existing payload contract. Upstream fuzzy whitespace matching is
accepted in a negative control; the experimental strict compiler rejects that
case, along with empty, absent, ambiguous and overlapping matches. We must not
silently adopt fuzzy matching or native direct-write semantics in the product.

## Recommended product repair (not implemented here)

1. Preserve Pi system-message sections in the Ollama adapter, including text-block
   content and later updates/removals. Test against pinned upstream rendering.
2. Replace model-authored diff with exact text replacements in the Pi tool. Read
   the immutable snapshot, validate all edits against the original bytes, and
   deterministically compile the candidate diff before existing action binding.
3. Keep the kernel `WorkspaceEdit {target_path, patch}` payload, digest binding,
   host `git apply --check`, one terminal action, and C-05 settlement unchanged.
4. Verify negatives (empty/missing/duplicate/overlap, path escape, no-op, CRLF/BOM/
   no-final-newline) plus real Pi CLI, then perform one bounded full Linux run
   under its explicit execution authorization. Success requires SUCCEEDED, one
   settled action, archived diff, seven independent tests and no post-edit model
   call. Do not mark E done based on this probe.

This is a change to the Pi-facing tool API, requiring the project interface
approval gate before product implementation. The diagnostic files are not
imported by runtime code and introduce no product dependency.

## Reproduce offline only

From this branch, with the existing pinned local carrier:

```sh
docker run --rm --network none --read-only \
  --mount type=bind,source="$PWD",target=/src,readonly \
  substratum/castor-pi-carrier:t337g \
  node /src/experiments/t337h/probe.mjs > /tmp/t337h-probe-recheck.json
python3 experiments/t337h/replay.py > /tmp/t337h-replay-recheck.json
diff experiments/t337h/probe-results.json /tmp/t337h-probe-recheck.json
```

The replay reads committed `probe-results.json`. Test stderr elapsed times may
vary; assertions and candidate/file hashes are the stable checks. Do not invoke
`live-probe.mjs` as part of offline tests or CI. It is an explicit live operation.
