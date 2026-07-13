# Task 1 — S-HITL Secondary Workload Report

## Status

Implemented and committed the S-HITL Paper A secondary workload in
`77911f45b8ce43b42610532973996b9b6fb23bb9`
(`feat(evals): add S-HITL workload`).

## Files changed

- `src/castor/evals/paper_a/secondary_workloads.py` — added
  `SecondaryWorkloadResult` and `run_s_hitl_workload`; the workload suspends a
  destructive payment for HITL, resolves it according to the supplied decision,
  resumes the checkpoint, and reports actuator/journal observations.
- `tests/test_paper_a_matrix.py` — added approve and reject S-HITL behavioral
  tests.

## TDD evidence

### RED

Command:

```console
PYTHONPATH=src /Users/yong/projects/substratum/castor/.venv/bin/python -m pytest tests/test_paper_a_matrix.py -k s_hitl -v
```

Output before production code:

```text
collected 0 items / 1 error
E   ModuleNotFoundError: No module named 'castor.evals.paper_a.secondary_workloads'
=============================== 1 error in 0.23s ===============================
```

This was the expected collection failure after adding the new import and tests:
the workload module did not exist.

### GREEN

Command:

```console
PYTHONPATH=src /Users/yong/projects/substratum/castor/.venv/bin/python -m pytest tests/test_paper_a_matrix.py -k s_hitl -v
```

Output:

```text
tests/test_paper_a_matrix.py::test_s_hitl_approve_has_one_payment_and_no_duplicates PASSED
tests/test_paper_a_matrix.py::test_s_hitl_reject_executes_no_payment PASSED
======================= 2 passed, 7 deselected in 0.19s ========================
```

## Full verification

```console
PYTHONPATH=src /Users/yong/projects/substratum/castor/.venv/bin/python -m pytest tests/test_paper_a_matrix.py -v
```

```text
============================== 9 passed in 3.52s ===============================
```

```console
/Users/yong/projects/substratum/castor/.venv/bin/python -m ruff check src/castor/evals/paper_a/secondary_workloads.py tests/test_paper_a_matrix.py
/Users/yong/projects/substratum/castor/.venv/bin/python -m ruff format --check src/castor/evals/paper_a/secondary_workloads.py tests/test_paper_a_matrix.py
```

```text
All checks passed!
2 files already formatted
```

## Self-review

- Approval executes exactly one deduplicated external payment and replay uses
  the HITL journal record rather than issuing a second payment.
- Rejection records `HITL_REJECTED`; replay completes without invoking the
  actuator, so zero effects are committed.
- The implementation is confined to the requested workload module and matrix
  test file; no ledger, plans, or unrelated project files were changed.
