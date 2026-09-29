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
