# T-372 clone128 repair — 2026-09-28

The input producer now builds standalone, shallow repositories for the exact
pinned commits. The previously blocked full FileBridge preflight succeeds with
zero real model calls. This fixes preparation; the live demo is still pending.

## Confirmed cause

The exact failing command was reproduced under the same sanitized Git environment:

```text
git clone --no-checkout --no-local /private/tmp/t372-standalone-input/upstream <fresh-target>
remote: fatal: unable to read 2112a5f915ab93fa5abb784a4bb1fd3485823328
error: git upload-pack: git-pack-objects died with error.
fatal: early EOF
fatal: fetch-pack: invalid index-pack output
```

The cached original repository had `remote.origin.promisor=true` and
`remote.origin.partialclonefilter=blob:none`. Its packs omit historical blobs.
The old producer first used the local-clone optimization, copying incomplete
packs without copying the source's promisor configuration. The resulting
intermediate repository failed connectivity checking. A subsequent full-history
transport clone tried to read those missing blobs and exited 128.

This is a preparation error, not evidence of a Docker socket or network failure.
The earlier external-worktree-gitdir issue is separate. Captured stderr was also
hidden by the command helper's default CalledProcessError rendering, obscuring
the useful diagnosis.

## Repair and boundaries

- Create a fresh repository and fetch the fixed SHA at depth 2 for upstream
  comparison (the original fixing commit plus its own parent).
- Fetch each selected task/reference commit at depth 1 into its own `.git`
  directory. No local hardlink clone, alternates, external gitdir or unrelated
  historical blobs are required. The reference repair commit is not in target.
- Keep the scrubbed Git environment, disabled hooks/fsmonitor, raw staging and
  archive extraction. Do not enable lazy fetching or run target code on host.
- Surface command-helper exit code and stderr. No retry/fallback masks errors.

This works when the selected commit closure exists. Missing required objects
remain a hard error. The cached repository is not rewritten or repaired globally.

## Verification

Two regression tests were observed failing before implementation. They now
verify isolation from missing unrelated history, validity after removing both
source repositories, exclusion of the reference commit, and useful Git errors.
Git/preparation suite: 4 passed; existing bridge/recovery suite: 23 passed.
Repository lint and format checks passed. No full CI result is claimed here.

The producer succeeded from both the existing partial cache and fresh public
HTTPS, preserving target tree `c20df619d8514b7b5c55af93bb3da7eab9251735` and all
frozen source/test/wheel hashes. Both standalone repositories passed `git fsck`.
The controller packed `/project` offline with no other repository mounts.

The full physical preflight used real Pi, FileBridge, a real loopback HTTP test
server, native Core settlement and Rust IsolatedVerifier. It returned SUCCEEDED,
12 protected pytest tests passed, two stub POSTs reserved before dispatch, zero
provider calls, and CLEAN cleanup. The only remaining Docker container was the
unrelated pre-existing `epic_agnesi`, left untouched. Read-only evidence audit
verified 14 journal frame CRCs, 8 Region content hashes and journal references;
this is not independent semantic replay.

Evidence is in [evidence/clone-repair](evidence/clone-repair/SHA256.json).
Raw state: `/private/tmp/t372-pinned-bridge` (fresh task ID
`task-t372-bits-bridge-pinned`). Old failed evidence is preserved in
`evidence/r1-stopped`. Existing T-365 results and denominators are unchanged.

## Remaining T-372 work

Finish current protocol/runtime pins and independent R2 bridge/gate acceptance,
then release the already approved single bounded real-model trial and audit its
result. This repair does not grant live release or claim a model repair success.

## Narrow physical review and factual correction

Physical Grok R1 returned REVISE, claiming both fetch hops and the regression
must fail for non-tip SHA wants. That blocker is contradicted by local execution,
the fresh HTTPS run, and all seven CI jobs at source842320a
([run36531944759](https://github.com/substratum-labs/castor/actions/runs/36531944759)).
A fresh diagnostic fetch from the staging repository with **no advertised refs**
and **neither uploadpack allow-SHA option configured** exited0. Packet trace
confirms protocol v2. See review-counterprobe.json and the unmodified R1 verdict.
Git's [v2 parse_want implementation](https://github.com/git/git/blob/master/upload-pack.c)
checks object existence without the v0/v1 ref-tip gate. No permission-relaxing
configuration was added. This correction does not imply independent acceptance.

The review's local packObjectsHook warning is also inapplicable: Git only honors
that key in protected configuration, as specified by
[git-config](https://git-scm.com/docs/git-config#Documentation/git-config.txt-uploadpackpackObjectsHook),
and the helper scrubs global/system/inherited command configuration. Remaining
nonblocking comments concern unchanged archive semantics, incomplete stderr
coverage outside the helper, broader protocol pinning, and portability checks.
Reference untracked verifier inputs are intentional; target alone is committed
and packed. Source deletion/fsck demonstrates self-containment, not nlink proof.
No generic support for arbitrary remote servers or old Git protocols is claimed.

Physical reviewer clarification subsequently returned **ACCEPT for this narrow
packaging repair**, withdrew the ref-tip blocker, and confirmed the local-hook
warning does not apply. The original nonblocking comments remain nonblocking.
See [actual clarification](evidence/clone-repair/GROK_PACKAGING_CLARIFICATION.md).
The reviewer process exited0 and was collected. No production code changed
between the first verdict and acceptance. Whole-bridge R2/live release remain
outside this acceptance.
