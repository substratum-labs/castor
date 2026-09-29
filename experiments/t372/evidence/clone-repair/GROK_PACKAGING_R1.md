**REVISE**

This verdict is only for the T-372 input-packaging repair. It does not accept the R2 bridge or a live release.

**Blocker:** `fetch_commit` runs `git fetch --no-tags --depth=N <repo> <raw-sha>` and never creates a real ref. Stock `upload-pack` serves a SHA only when it is an advertised ref tip, or when that serving repo has `uploadpack.allowAnySHA1InWant` / `uploadpack.allowReachableSHA1InWant`. `git_environment()` sets `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`, and the local `upload-pack` child inherits that, so a system or global opt-in cannot apply. The partial cache and the fresh upstream repo leave those keys unset.

- Local cache hop: pinned `d0e36b4…` is an ancestor of the boltons default branch, not a tip. The depth-2 fetch exits 128 (`not our ref` / unadvertised object).
- Second hop: a GitHub fetch can store the objects, but only as dangling objects plus `FETCH_HEAD`. `upload-pack` advertises refs; `FETCH_HEAD` is not one. `checkout_without_filters` then fetches `PARENT` and `FIX` from that local repo and hits the same refusal. No self-contained target or reference `.git` is produced.
- `test_snapshot_ignores_missing_old_blob_and_survives_source_removal` raises `RuntimeError` at `checkout_without_filters(staging, project, wanted)`. `wanted` is only a parent, so the fsck, alternates, and reference-exclusion assertions never run. The test also never builds a `blob:none` promisor repo or fetches a non-tip SHA from a repo whose only tip is a later commit.

Depth limiting is the right closure (unrelated missing blobs stay outside the pack). The want is not servable, so partial-cache and HTTPS snapshots are still broken. For a local source, the serving side needs `uploadpack.allowAnySHA1InWant=true` (for example `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*` on that fetch, inherited by `upload-pack`) or a real ref at every SHA that will be fetched next. `allowReachableSHA1InWant` is not enough on the upstream repo: after the first fetch it has no ref tip that can reach `PARENT`.

**Nonblocking:**
- `git archive` and `hash-object` still use `check=True`, so those failures omit stderr. The archive call also skips `core.fsmonitor=false`.
- `git archive` still applies in-tree `export-ignore`, `export-subst`, and `ident`.
- Target tree `c20df619d8514b7b5c55af93bb3da7eab9251735` is recorded, not enforced. `verify_original_mathutils.sh` is not hash-pinned.
- Reference stays dirty (wheels and verify script untracked). Only target is committed.
- `fsck` after deleting the source does not prove `st_nlink == 1`; hardlinked objects survive `rmtree` and still fsck.
- `run()` uses strict text mode, so non-UTF-8 stderr becomes `UnicodeDecodeError`. The stderr test also depends on English `does not exist`.
- A local cache can still run `uploadpack.packObjectsHook` from its own repo config on the first hop.
