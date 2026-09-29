# Trusted slot management launcher

`scripts/run_trusted_slot.py` runs the native Linux `castor run --project` frontend
inside a trusted, networkless Docker controller. It is an opt-in management/test
entrypoint, not a Python product runtime dependency. Existing native/macOS CLI
semantics, Rust Core, Pi extension, and protected `IsolatedVerifier` are unchanged.

The deterministic model fixture supports `defect.txt` containing `failing fixture`.
`fixing` and `nonfixing` first request a real `castor_read_file` of `defect.txt`.
The next request must contain its successful `failing fixture` read observation
before an edit is emitted: `fixed fixture` or `still failing fixture`, respectively.
The native extension ends locally after the terminal edit, so completed runs use
two model interactions, one patch action, and one settlement. `timeout` stalls the
first native model interaction.
The mock model service uses a framed Linux Unix socket inside the controller.
T-372 adds a separately gated real-model mode: the same-kernel controller UDS
bridges through durable state files to a trusted-host HTTP pump. Pi and verifier
still have no network. See [T-372 preparation](../experiments/t372/README.md).
Only the actual native `IsolatedVerifier` executes the target verification command.

## Invocation

Use a clean committed Git fixture, a task spec accepted by the native project
frontend, and a fresh durable state directory outside `/run` and Docker's root.
The launcher injects the explicit task/idempotency IDs into its private spec copy;
it does not alter the input project or task spec.

```sh
python3 scripts/run_trusted_slot.py \
  --project /absolute/fixture --task-spec /absolute/spec.json \
  --task-id task-t371-example --idempotency-key task-t371-example \
  --state-root /absolute/durable/state --scratch-parent /run \
  --mock-mode fixing --linux-bin-dir /absolute/linux/debug \
  --trusted-controller-image sha256:39fad3ec792c6d55d6049f010520ebdc662c92c9eb8f6b74872b7842e073ba80
```

The T-372 fixture also accepts `--mock-mode bits_fixture` for a genuine Bits
read/edit/protected-pytest positive control. It has zero provider calls. The
separate `--model-mode ollama` path requires `--protocol-file` and
`--release-file`; no release file is included in the source tree, and a frozen
input/protocol/runtime gate runs before any inference. The proposed run is
awaiting Yong's model/budget selection and root release.

Optional flags: `--docker-bin` (resolved absolute host executable),
`--workload-timeout` (120 seconds), and `--cleanup-timeout` (60 seconds).
Both deadlines must be finite and positive. The workload clock starts at entry;
cleanup has a separate bounded budget. The state directory's `launcher` subdirectory
must be new so existing forensic evidence cannot be overwritten.

Prerequisites are Docker, standard-library Python on the management host, genuine
Linux `castor`/`castord` binaries, and these already-local immutable images:

- Controller: `sha256:39fad3ec792c6d55d6049f010520ebdc662c92c9eb8f6b74872b7842e073ba80`.
- Pi tag `substratum/castor-pi-carrier:v1` must resolve to
  `sha256:b4f363b3436653157b87428ec6d4936b9efa26c184a77d346345071d02edddb7`.
- Native verifier runtime lookup: `docker.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`.

The historical logical verifier pin in task receipts stays unchanged. Runtime
lookup uses its fully-qualified spelling; cleanup accepts either exact spelling
only with matching image ID and the existing private container profile.

Management creates use `--pull=never`; the native product derives its ordinary
fixture image from the local Pi base. This launcher does not rebuild/change the
base images. It has been tested against the local Docker Desktop Linux Engine;
remote Engine filesystem forwarding is not part of this fixture contract.

## Ownership, evidence, and outcomes

Trusted short-lived Engine helpers canonicalize paths in the Engine namespace,
check the queried `DockerRootDir`, and create an exclusive short scratch directory
with an owner marker. Scratch is bound at the identical source/destination and
exported as `TMPDIR`. State mapping comes from actual Docker inspect, including
Docker Desktop's Engine source translation. Helpers have a read-only Engine view
and a scoped writable scratch-parent mount; Pi/verifier keep their native single
readonly bind and isolated profile. The controller alone receives the management
Docker socket and durable state.

The launcher tracks exact CIDs, private mounts, and native child profiles. Labels
or names alone never authorize removal. On timeout it archives the live Pi child,
freezes recorded owned children to prevent IPC-close exit races, SIGKILLs the
controller first and then children, archives terminal inspect/logs, and removes
only recorded resources. Quiescence is observed before private scratch deletion.
Product-self-removed verifier containers are represented by native profile/logs;
they are not inspected after deletion.

Stdout is one management JSON envelope with `launcher_status`, `task_result`,
`cleanup_status`, `evidence_dir`, `controller_cid`, and `owned_cids`. Native task JSON
is copied unchanged, including omitted optional fields and `UNKNOWN_DISPUTED`.
Absent product output/result is null, never a fabricated kernel timeout reason.
`workspace_snapshot_sha256` is native bare 64-hex; image IDs and final patch hashes
retain their native `sha256:` prefix.

- Exit 0: SUCCEEDED and CLEAN.
- Exit 1: FAILED or TIMEOUT, with CLEAN teardown.
- Exit 2: RETAINED_FAILURE; durable state and scratch are retained when cleanup or
  archival cannot be proved. Docker transport failure is not container absence.

`state/launcher/` contains raw controller streams, terminal inspect, native result,
mount map, lifecycle events, admin CIDs/inspect/streams, cleanup/quiescence proof,
and mock request/response accounting. Timeout also preserves exact pre-kill child
inspect and terminal child logs. Native board, Pi log, journal, Regions, and verifier
logs stay at their native state paths. A remaining unknown private mount prevents
scratch deletion rather than granting permission for a sweep. Real daemon outages
can therefore require operator recovery using the recorded exact CIDs/owner token.

## Standalone tests

```sh
python3 -m unittest discover -s tests -p test_trusted_slot_units.py -v
python3 tests/test_trusted_launcher.py -v
# Alternatively, opt in to physical pytest execution:
CASTOR_TRUSTED_LAUNCHER_PHYSICAL=1 pytest tests/test_trusted_launcher.py -v
```

Ordinary `pytest tests/` does not collect the physical class. This is collection
isolation, not a skip: direct execution and explicit opt-in still fail on missing
prerequisites. Collection regression tests exercise default discovery, opt-in
discovery, and strict direct failure with Docker absent.

The physical suite has no dependency on the internal coordination repository.
`CASTOR_ROOT` can select another authorized source worktree; `LINUX_BIN_DIR`
defaults to `/private/tmp/t365-native-build/debug`. Physical checks require all
prerequisites and never skip a failed fixture. V1–V6 cover native success, truthful
nonfixing failure, real live-child timeout, three foreign containers unchanged
across success/timeout, Engine-root/symlink negatives and same-path native mapping,
and exact-CID transport-fault retention followed by scoped test recovery.
The fault wrapper affects only host controller removal, not native Docker.

The harness prints and preserves bounded host evidence directories. Its test-only
cleanup removes recorded test CIDs and owned Engine scratch after assertions;
V6 retains the original failed-cleanup evidence while separately recording its
successful test recovery. No unrelated container or historical study volume is
removed. Unit fixture `native-success.json` is an actual native result captured
while detecting and reviewing the snapshot-hash oracle correction.
