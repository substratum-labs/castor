# T-257 S-Bypass and L-Scaling

## S-Bypass negative control

Raw payment I/O is outside the Castor journal; duplicate effects are expected after crash recovery.

| system | fault | committed_effects | dup_commits | resume_success | commits |
|---|---|---:|---:|:---:|---|
| s_bypass | kill_after_commit | 3 | 1 | yes | payment, payment, email |

## Journal-length scaling

Prototype checkpoint/journal overhead measurements; no MMU or asymptotic claim.

| journal_len | journal_bytes | resume_ms | status | error |
|---:|---:|---:|---|---|
| 0 | 496 | 0.962 | COMPLETED |  |
| 4 | 1339 | 0.377 | COMPLETED |  |
| 16 | 3884 | 0.362 | COMPLETED |  |
| 64 | 14108 | 0.486 | COMPLETED |  |
| 256 | 55317 | 0.861 | COMPLETED |  |

## Reproduction

`python -m castor.evals.paper_a.overhead --out results/paper_a --label t-257 --lengths 0 4 16 64 256`
