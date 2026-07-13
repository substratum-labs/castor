# T-245 Task 3 — LangGraph `kill_after_success` Harness Race Fix

## Scope

Changed only the parent evaluation harness. The LangGraph worker, graph
topology/policy, Castor kernel behavior, operation IDs, caches, retries,
journal, `TrialResult`, and task ledger are unchanged.

## Root Cause

`post_payment` emits `ACTUATOR_COMMITTED` before the LangGraph execution has
necessarily committed the checkpoint produced by the preceding `payment` node.
The parent previously issued `SIGKILL` immediately after reading that marker.
If the process died during this gap, resume re-entered `payment`, whose baseline
uses a fresh external operation ID, causing a duplicate payment.

Direct inspection through LangGraph's official `SqliteSaver` after the marker
found five checkpoints for the trial thread. The durable payment transition had
both `payment` and `branch:to:post_payment` in its persisted
`channel_values`; resuming from that state executes only `post_payment` and
`email`.

## RED Evidence

Before the change, the existing exact regression was run six times with:

```sh
UV_CACHE_DIR=/private/tmp/castor-uv-cache uv run pytest -q \
  tests/test_paper_a_matrix.py::test_langgraph_kill_after_success_keeps_checkpointed_payment
```

Results: 4 failures and 2 passes. Each failure asserted `3 == 2` for
`committed_effects`, with commits `('payment', 'payment', 'email')` and
`dup_commits=1`. This is the target race, not a test setup error.

## Change

After the existing marker is received, only for
`system == "b_langgraph"` and `fault == "kill_after_success"`, the parent
polls `SqliteSaver.list()` for the same `thread_id`. It kills the worker only
after one durable checkpoint has both of these channels:

- `payment`
- `branch:to:post_payment`

The poll is condition-based (10 ms polling interval), not a post-marker sleep.
It has the existing 15-second bound and raises an error that includes the
thread ID, checkpoint database path, and last seen channel names if the
condition never becomes durable.

## Files

- `src/castor/evals/paper_a/harness.py` — imports the official `SqliteSaver`,
  waits for the durable payment checkpoint before the targeted SIGKILL, and
  reports a clear timeout.
- `.superpowers/sdd/task-3-fix-report.md` — required Task 3 evidence report.

No test file change was necessary: the pre-existing focused integration test
is the exact regression and was first observed failing, then run repeatedly
after the fix.

## GREEN Evidence

The exact regression passed six consecutive times after the change:

```sh
UV_CACHE_DIR=/private/tmp/castor-uv-cache uv run pytest -q \
  tests/test_paper_a_matrix.py::test_langgraph_kill_after_success_keeps_checkpointed_payment
```

Each run: `1 passed` (six of six).

Focused verification:

```text
UV_CACHE_DIR=/private/tmp/castor-uv-cache uv run pytest -q tests/test_paper_a_matrix.py
10 passed in 5.60s

UV_CACHE_DIR=/private/tmp/castor-uv-cache uv run ruff check src/castor/evals/paper_a/harness.py tests/test_paper_a_matrix.py
All checks passed!

git diff --check
(exit 0)
```

## Self-Review

- The gate is limited to the one specified baseline/fault pair; all other
  systems and faults preserve their prior kill timing.
- It observes the checkpointer from the parent rather than adding synchronization
  or durability behavior to the evaluated graph.
- It checks the same LangGraph thread ID (`pid`) and the precise scheduled
  post-payment branch, so it cannot accept a checkpoint from another trial or
  merely an earlier payment write.
- No arbitrary fixed delay is used to make the test pass.
- The task artifact is the only non-source file added; the pre-existing
  `.superpowers/sdd/task-1-report.md` modification was not changed.
