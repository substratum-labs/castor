# T-372 live trial r1 — failed, no repair delivered

2026-09-29. Both actual physical Grok R2 scopes ACCEPTED the frozen bridge and
execution gate. Root released and ran exactly one registered local Qwen trial.
This is a real model failure, not a zero-model preflight or a delivered demo.

## Result and cost

- TaskResult: FAILED / MODEL_INTERACTION_ERROR; launcher exit1 in23.136 seconds.
- Actual inference:3 HTTP POSTs, all200/done=true/model=qwen3.5:9b; all three
  durable reservations COMPLETED. Input tokens2824; output tokens145 (65+40+40).
  Requested per-call output cap512, total unique-call cap3. Local API fee$0;
  hardware/energy and external review costs were not measured.
- Fourth unique interaction was denied before POST. The native worker repeated
  that denied UDS request three times; it did not trigger extra inference. Six
  bridge request files represent four unique interactions and only three POSTs.
- No TurnCommitted, ActionRegistered or AttemptSettled; no patch or code effect.
  Twelve journal CRCs/eight Region hashes/references verified; FenceRevoked1.
  This is integrity/count reconciliation, not semantic journal replay.
- CLEAN cleanup: task controller/Pi removed, scratch absent, host process group
  empty, and the pre-existing container inventory preserved. No live verifier
  was reached because the agent never submitted an edit. Independent verifier
  replay afterwards was also removed.

## Observed sequence and diagnosis

| Model request | Tool request | Tool result |
| --- | --- | --- |
| 1 | castor_read_file /workspace/Boltons_Bits.py | workspace path must be relative |
| 2 | castor_read_file Boltons_Bits.py | file does not exist |
| 3 | castor_read_file . | workspace path escapes snapshot |
| 4, denied | another model interaction, after tool error | unique HTTP call budget exhausted |

The native request offered only castor_read_file and one terminal castor_edit_file.
It offered no directory/search tool or repository inventory. The task prompt
identified the behavior but omitted the known implementation/test paths. The
model violated the relative-path rule, guessed a nonexistent filename, then tried
to discover files through a regular-file reader. It never saw source or tests.

The bridge faithfully delivered requests, tool errors and responses; usage,
model metadata and budget enforcement worked. This run does not measure the
model's ability to repair the bug once given the implementation. Missing discovery
context is a concrete input-contract gap. Whether path context is sufficient for
this model remains a hypothesis for a separately approved trial.

The unchanged frozen target tree and original test/wheel/spec hashes remain
intact. A fresh independently compiled native Rust executable replayed the
original command through IsolatedVerifier:1fail/11pass, as expected. No model
candidate existed; this replay is explicitly the unchanged authoritative input.

## Reviewable next experiment, not released

[proposed-task-spec-r2.json](proposed-task-spec-r2.json) adds only exact relative
implementation/test paths and tool-usage guidance, with no reference repair.
Its SHA256 is `cb6834c5a88d99c3c236b12e00510d6f0160eae1f33c086ae1e77ba24618ff73`; use the actual file hash recorded at checkin.
Keep the same model/digest/options, <=3 inference attempts, <=512 output tokens,
300s/60s and no retries. Use a fresh r2 identity/state; update the test launcher's
identity binding, refreeze its runtime/spec hashes and obtain a focused delta
review before any new release. Preserve this failed trial unchanged.

This proposal has no live release or new budget approval. The agreed execution
plan requires a failed trial to be archived and returned for a new decision.
Generic automatic repository inventory in the agent context is a follow-up UX
contract; adding a shell or loosening sandbox paths is not required by this fix.

Full evidence and SHA256 inventory: [evidence/live-r1](evidence/live-r1/SHA256.json).
Raw durable state: /private/tmp/t372-bits-live-r1. No second model trial was run.

Actual physical Grok outcome audit exited0 and ACCEPTED the honest failure
characterization, accounting and path-context proposal. It explicitly leaves
T-372/demo unfinished and any r2 trial unrun/unapproved. Its limits: journal
integrity counts are the owner's verified check, not semantic replay; detailed
pytest split is in independent-verifier.stdout, while the structured result
contains exit1/reason/profile. Raw POST bodies in bridge/http-request-*.raw and
frozen runtime prove the configured caps; realized responses also stay below512.
See evidence/live-r1/GROK_OUTCOME_AUDIT.md. No new live release is implied.
