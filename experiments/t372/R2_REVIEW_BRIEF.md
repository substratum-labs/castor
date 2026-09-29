# T-372 physical R2 brief

Date: 2026-09-29. Status: FROZEN FOR REVIEW; no live release, no model inference.
Review the bounded Bits-demo bridge and execution gate. Existing Core, native
Pi frontend and IsolatedVerifier semantics remain unchanged. Yong approved one
local qwen3.5:9b trial, <=3 unique HTTP attempts, <=512 output tokens/call,
300s workload plus60s cleanup, no retries, think=false/temp0.2/seed17/ctx32768.
Fresh live identity: task-t372-bits-r1, state /private/tmp/t372-bits-live-r1.

Frozen protocol SHA256:
`5c421d71ce311d6b38d631130dd9a1077da8eb0ab04171b7e5f225c7f538a7db`.
The protocol now includes all ten current runtime-file hashes, original input
hashes, exact images/native ELF hashes, preparation script hashes and fresh
source-manifest digest. Public frozen copy and refreshed tests are archived in
evidence/r2-preparation/. No acceptance receipt or live release exists yet.

## Physical evidence already obtained

- Original protected oracle: pre-fix genuine pytest 1fail/11pass, reference12pass;
  IsolatedVerifier profiles/image/cleanup recorded in evidence/oracle/.
- New actual Pi → same-kernel UDS FileBridge → host exchange → REAL loopback HTTP
  test server → Pi read/edit → Core settlement → IsolatedVerifier: SUCCEEDED,
  12 pytest passes, 2 stub POSTs with durable reservation before dispatch,
  0 provider calls and CLEAN cleanup. Full raw state /private/tmp/t372-pinned-bridge;
  bounded results in evidence/clone-repair/. This directly exercises the new
  bridge and real HTTP transport; it is not the old MockModel-only preflight.
- Read-only integrity audit:14 valid journal CRCs,8 Region content hashes and
  referenced digests; 1TurnCommitted and1AttemptSettled. This is not semantic replay.
- Refreshed23 bridge/recovery units and4 preparation/safe-Git tests pass. The
  existing live-gate test now additionally rejects nine altered protocol pins
  even when the approval/release hashes are recomputed to match the changed file.
  Prior code842320a CI passed7/7, including Linux/Python3.11/3.12/3.13.
- Fresh standalone live inputs from public HTTPS reproduce original frozen tree
  c20df619d8514b7b5c55af93bb3da7eab9251735. The protected test command is a tracked
  file; full raw HEAD/index/filesystem binding rejects altered or extra files.

## R1 punch-list disposition

ScopeA:
1. Budget reset fixed: exclusive budget creation, fsync file/directory; any old
   ledger fails closed. After worker reap, uncertain RESERVED becomes FAILED.
   This is deliberate refusal to restart, not general process recovery support.
2. Absolute deadline fixed: exchange runs in its own forked process; parent
   watchdog stays responsive, timer terminates/reaps HTTP worker at total cap.
   FileBridge has deadline, closes partial reads, sends bounded framed errors.
3. Pin checks now run before and after every real POST. Response model and done
   must match. A 200 body with missing usage or >512 output fails, consuming its
   reservation. No retries. Metadata GETs are read-only, not inference calls.
4. Unsupported native content shapes fail before transport rather than silently
   disappear. Original Pi system sections/tool-result semantics are preserved.
5. Only trusted controller has durable bridge mount. Untrusted Pi gets a single
   IPC socket; verifier gets candidate only. Neither can create bridge files.

ScopeB:
1. Raw safe_git HEAD/index/filesystem plumbing with scrubbed environment, fixed
   /usr/bin/git, disabled hooks/fsmonitor; no porcelain status or inherited Git
   directory/config. Safe-Git tests cover actual fsmonitor/inherited GIT_DIR risks.
   Aliases cannot replace Git builtins; extdiff/textconv claims were overstated.
2. Correct64-char reference SHA is recomputed from a separate reference workspace.
3. Gate checks exact image/wheel/runtime/native-binary pins, selected model/options,
   caps, clean target tree, original test/spec/source hashes and bound root release.
4. Lifecycle starts/reaps HostExchange even on start failures; watchdog does not
   call blocking poll. Constants3 and512 match gate-enforced frozen caps.
5. Byte-pinned retained binaries are NOT cryptographic build attestation. This
   limitation is recorded in frozen protocol. Core/carrier source is unchanged
   from previously approved native build source; no historical volume is reused.

Preparation clone issue is independently accepted after factual clarification:
Git v2 allows existing-object wants from ref-less staging repos; no allow-SHA
override was added. Historical failed attempts and original review verdicts are
retained. No global cache mutation, host target execution or broad cleanup.

## Trust and release boundary

This trial assumes a trusted operator-controlled local Ollama and trusted
management controller/host. Pre/post tags+version and response.model/done bind
observable metadata, not adversarially swapped model weights cryptographically.
Atomic JSON writer already fsyncs file/directory; no torn-ledger rewrite was needed.
The reviewer acceptance file is a root-written receipt of actual physical review,
not a self-acceptance or signature against a malicious root administrator.

After both scopes ACCEPT, root freezes the existing exact bytes, writes one
receipt and one release bound to protocol hash/state/task/budget, validates gate,
then launches exactly once. Any live failure is archived honestly; no second
attempt is implied. Actual candidate will be reconstructed from TaskResult patch,
input tree and hashes, then the original pytest command replayed independently
through IsolatedVerifier. Accounting reconciles raw HTTP bodies, reservations,
Core journal/Regions, TaskResult, original oracle and exact owned-CID cleanup.
This is a historical public bug demo; no blind benchmark or reliability estimate.
