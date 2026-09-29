# T-377 repair evidence

Initial short-reference inspections failed; canonical inspection succeeded with
the same image identity/RootFS. The subsequent matrix and unchanged native observer
passed. This state change is recorded without claiming Docker cache internals are
known. `unchanged-observer-after-canonical.stdout` is a PASS, not the RED regression.

`canonical-regression-red.*` and `aliases-red.*` are witnessed old-code failures;
`verifier-green.*` and `management-green.stderr` are fixed-code regressions.
`kernel-tests.*` records the initial macOS default-temporary-root no-follow failures;
`kernel-canonical-tmp.*` uses a canonical `/private/tmp/t377-test-tmp` and passes
309 tests with 6 ignored. No path guard was relaxed.

`oracle-result.json` comes from a fresh public source/wheel input and locked native
build. Red is the original baseline (1 failed/11 passed), green is the actual r2
patch candidate (12 passed). Candidate proof inventories every source file; tests,
wheels and verification command are unchanged. Both containers removed; inventory
unchanged. `verification-summary.json` checks original r1/r2 budget hashes unchanged.
No model bridge/agent started; zero provider calls. The initial Docker `commands.json`
contains argv/status, with representative lookup stdout/stderr retained here.
