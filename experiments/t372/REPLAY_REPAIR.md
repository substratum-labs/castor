# T-377: zero-model candidate replay repaired

The fresh command in [DEMO_WALKTHROUGH.md](DEMO_WALKTHROUGH.md) rebuilds the actual
T-372 r2 candidate and passes its original 12 tests with **zero provider calls**.
The unchanged baseline fails 1 test and passes 11. Both native verifier containers
are removed; the measured run preserves the prior container inventory.

## Diagnosis and bounded correction

On the recorded Docker Desktop 4.66.1 / Engine 29.3.1 instance, initial read-only
inspection of `python:3.12-slim`, `python@sha256:…` and the frozen short tag+digest
reference returned `No such image`. Inspection by image ID succeeded. Inspection
of `docker.io/library/python@sha256:…` then succeeded with the same Id, RepoDigests,
OS, architecture and RootFS. Complete inspect JSON differs in optional/default
metadata, so byte-for-byte JSON equality is not claimed.

After that successful fully-qualified lookup, the short lookups and an unchanged
native observer also succeeded. This was state-dependent image-reference lookup;
we have not established which Engine indexing/cache mechanism caused the change.
The retained native observer output is a **passing diagnostic**, not a RED proof.
Initial short-name errors, subsequent matrix and identity comparison are preserved.

The application defect is reliance on implicit repository aliases at image
preflight. Native verifier inspect/create/profile checking and project-run
preflight now use the complete `docker.io/library/` reference with the original
sha256 digest. The project receipt's logical pin and task-identity derivation are
unchanged. Trusted management uses the same complete lookup and recognizes both
exact historical/current Config.Image spellings, only alongside the exact actual
image ID and existing private-mount/container profile checks.

No replacement image, host target execution or weaker isolation is introduced.
Docker documents default registry/namespace expansion in its
[image reference guidance](https://docs.docker.com/reference/cli/docker/image/tag/).
The fix removes dependency on that implicit lookup in this path; it does not
claim a Docker-wide short-name bug or a fully explained cache fault.

## Verification

- Existing native verifier baseline: 3 tests passed.
- Witnessed RED: a scripted engine accepting only the fully-qualified pinned
  reference prevents the old verifier reaching execution; canonical cleanup
  alias is rejected by the old management code.
- GREEN: verifier safety scenarios plus positive completion pass; cleanup tests
  accept only the two exact pinned spellings and reject foreign names, wrong
  actual IDs and foreign scratch paths.
- Fresh public source and wheel preparation, exact archived patch reconstruction,
  new locked native build, genuine pytest in IsolatedVerifier: baseline 1 failed /
  11 passed, actual candidate 12 passed. Two containers removed, inventory unchanged.
- Native suite: 309 passed / 6 ignored with a canonical temporary root; targeted
  Python suite: 44 passed / 17 subtests; strict all-target clippy passed. Initial
  default-temp-root tests failed on macOS no-follow path validation; rerun uses
  `/private/tmp/t377-test-tmp` and does not weaken the path guard.
- Raw artifact root: `/private/tmp/castor-t372-replay.SzLu0i`.
  Source evidence inventory: [evidence/replay-repair/SHA256.json](evidence/replay-repair/SHA256.json).

The original failed recipe evidence and r1/r2 budgets remain unchanged. The
provider gate still checks frozen runtime hashes; these source changes are not
an authorization or reusable release for another model trial. Generic workspace
discovery and user-facing runtime integration remain separate work.
