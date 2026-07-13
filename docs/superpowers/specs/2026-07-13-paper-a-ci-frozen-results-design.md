# Paper A CI Smoke and Frozen Results

## Purpose

Make the S-Pay evidence reproducible without turning every pull request into a
full experiment.  CI will establish that E0 and the safety-critical C-full
path work; a committed, labelled N=3 matrix will preserve the Paper A evidence
used by the writing workflow.

## Scope

- Add a Python 3.11-only CI smoke job.
- Run the existing E0 test coverage and a C-full-only S-Pay matrix over
  `kill_after_commit` and `kill_after_success`, with three trials per cell.
- Add one documented full-run command for all existing systems and both faults
  at N=3.
- Commit the resulting `results/paper_a/results.json` and `results.md`, with
  provenance that identifies the run as `full-n3` and records the command,
  commit, and timestamp.

## Design

The matrix runner remains the single producer of result rows.  Its output adds
a small run-manifest alongside the existing JSON and Markdown table, rather
than changing the `TrialResult` row schema used by current tests and Paper A
comparisons.

The CI workflow installs the existing `paper_a_eval` extra, runs the focused
E0 test selection, then invokes the matrix module with `--systems c_full`,
both declared faults, and `--trials 3`.  CI writes only to a temporary output
directory; it does not update frozen artifacts.

The README shows a one-line full command.  It regenerates the frozen artifact
directory and produces a manifest labelled `full-n3`; its 30 trials are the
five supported systems times two faults times three repetitions.

## Boundaries and verification

This task does not add workloads, alter recovery semantics, or expand Paper A
claims.  The full run is a reproducibility artifact, not a statistical claim
beyond its documented N=3 sample size.  Verification covers the new unit
tests, focused CI command, complete test suite, Ruff checks, and a fresh full
matrix whose committed artifacts are read back.
