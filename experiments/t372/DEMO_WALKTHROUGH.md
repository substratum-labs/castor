# T-372 demo walkthrough

Read [r2 report](LIVE_TRIAL_R2_REPORT.md) alongside the actual
[TaskResult](evidence/live-r2/task-result.json), [model patch](evidence/live-r2/actual.patch)
and [original protected pytest log](evidence/live-r2/verifier_stdout.log).

1. Input: authentic Boltons pre-fix parent plus original upstream regression tests,
   offline pytest wheels and protected verifier. Baseline: 1 failed / 11 passed.
2. Task: repair Bits negative indexing; explicitly name source and test paths.
   Native Pi reads both through Castor. The separate reference is not agent input.
3. Model: Qwen returns a terminal source edit after two real calls. Castor commits
   one turn/action and verifies the candidate in the protected networkless container.
4. Output: TaskResult SUCCEEDED, exact patch, 12 original tests pass, cleanup CLEAN.
5. Independent audit: rebuild the candidate from frozen input + actual patch; all
   other files remain identical. Separate IsolatedVerifier replay also passes 12.

## Offline replay of the actual patch

No model call or consumed release is needed. From the source repository, prepare
fresh input with `python3 experiments/t372/prepare_source.py --output /private/tmp/t372-demo-replay`.
Copy its target into a separate candidate, then apply `evidence/live-r2/actual.patch`
using Git's fixed/sanitized configuration as used by preparation. Do not run
upstream code on the host. Copy `evidence/live-r2/independent-oracle.rs` as
`src/main.rs` and its `independent-oracle.Cargo.toml` as `Cargo.toml` in a temporary
crate; update only its absolute Castor kernel dependency path to this checkout.
The observer accepts workspace and fresh output directory as positional arguments
(see its source), and invokes native `IsolatedVerifier` with the pinned image.
Build/run with Cargo using Docker Desktop; preserve candidate tests, wheels and
`tests/t372_verify_original_mathutils.sh`. The supplied independent-result.json and
independent-verifier.stdout are the recorded replay, not a promise of local setup.

Images/binary pins and historical exact live command are in frozen-protocol-r2.json
and evidence/live-r2/command.json. These document the consumed run, not permission
to execute another live trial. A new live demo needs fresh identity/state and a
new reviewed release/budget. Generic workspace discovery remains separate work.

## Concrete candidate replay commands (currently environment-blocked)

The fresh-source recipe below reproduced input and compiled, but three verifier
invocations stopped at VerifierUnavailable because local Docker could not resolve
the pinned repository reference. It is not an end-to-end passing recipe on the
current environment. Stop if pinned-image inspect fails; no fallback image.
The prior recorded independent replay did pass; see the r2 report.

Prerequisites: Python/pip for source and wheel preparation, Rust/Cargo, Docker
Desktop, and the pinned verifier image available locally. These commands download
public source and wheels but make **zero model calls**. Use fresh paths or stop
if any output path exists. Preparation validates wheel hashes against the pins.

```sh
docker image inspect python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
python3 -m pip download --only-binary :all: --no-deps \
  --dest /private/tmp/t372-demo-wheels \
  pytest==8.3.5 iniconfig==2.0.0 packaging==24.2 pluggy==1.5.0
python3 experiments/t372/prepare_source.py \
  --wheel-dir /private/tmp/t372-demo-wheels \
  --output /private/tmp/t372-demo-replay
python3 - <<'PY'
import shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from trusted_slot.safe_git import git_environment
candidate = Path('/private/tmp/t372-demo-candidate')
shutil.copytree('/private/tmp/t372-demo-replay/target', candidate)
patch = Path('experiments/t372/evidence/live-r2/actual.patch').resolve()
subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null',
                '-c', 'core.fsmonitor=false', 'apply', str(patch)],
               cwd=candidate, env=git_environment(), check=True)
crate = Path('/private/tmp/t372-demo-observer')
(crate / 'src').mkdir(parents=True)
shutil.copyfile('experiments/t372/evidence/live-r2/independent-oracle.rs',
                crate / 'src/main.rs')
manifest = Path('experiments/t372/evidence/live-r2/independent-oracle.Cargo.toml').read_text()
manifest = manifest.replace('/Users/yong/projects/substratum/castor/.worktrees/t372-bits-demo/kernel',
                            str(Path('kernel').resolve()))
(crate / 'Cargo.toml').write_text(manifest)
PY
cargo run --manifest-path /private/tmp/t372-demo-observer/Cargo.toml -- \
  /private/tmp/t372-demo-candidate /private/tmp/t372-demo-verifier-evidence
```

Expected exit 0, reason NONE and `12 passed` in the fresh evidence directory's
`verifier_stdout.log`. The Cargo observer compiles trusted verifier code; the
candidate's code/tests run only inside its isolated Docker verifier. The logs
record the inspected image/profile and exact removal. This recipe may require
network access for public preparation and Cargo dependencies, not for inference.
