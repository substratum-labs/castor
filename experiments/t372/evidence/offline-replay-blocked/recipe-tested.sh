set -eu
export CARGO_TARGET_DIR=/private/tmp/t372-cargo-target
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
