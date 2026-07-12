# Task 1 — Independent SQLite ActuatorBench Report

## Status

Implemented the evaluation-only SQLite actuator in commit
`c201de15d6084817453497ee32e12db87a28e472`
(`feat(evals): add independent SQLite actuator bench`). The implementation is
isolated under `castor.evals`; it does not import or interact with Castor
checkpoint persistence.

## TDD evidence

### RED

Command:

```console
uv run pytest tests/test_actuator_bench.py -q
```

Output before production code:

```text
ModuleNotFoundError: No module named 'castor.evals'
1 error in 0.20s
```

This was the expected failure: the contract test imported the absent Task 1
module.

### GREEN

Command:

```console
uv run pytest tests/test_actuator_bench.py -q
```

Output:

```text
.                                                                        [100%]
1 passed in 0.13s
```

Focused quality checks:

```console
uv run ruff check src/castor/evals tests/test_actuator_bench.py
uv run ruff format --check src/castor/evals tests/test_actuator_bench.py
```

Output:

```text
All checks passed!
3 files already formatted
```

Full-suite command:

```console
uv run pytest -q
```

Output:

```text
511 passed, 9 skipped in 14.51s
```

## Changed files

- `src/castor/evals/__init__.py` — new evaluation package boundary.
- `src/castor/evals/actuator_bench.py` — SQLite-backed, idempotent actuator
  with `INSERT ... ON CONFLICT DO NOTHING`, stable commit-ID lookup, and
  `ActuatorMetrics`.
- `tests/test_actuator_bench.py` — contract test covering duplicate operation
  IDs, one persisted row, stable commit ID, and `dup_commits == 0`.

## Self-review

- `operation_id` is the SQLite primary key, so retries cannot create a second
  effect row.
- `BEGIN IMMEDIATE` contains the conflict-safe insert and lookup in one
  transaction; callers receive the previously stored commit ID after a retry.
- The database path is supplied directly to the bench and the module has no
  dependency on `castor.scheduler.persistence`, checkpoints, workers, or a
  harness.
- Scope is limited to the three requested Task 1 code/test files plus this
  requested report. No worker or kill harness was added.
