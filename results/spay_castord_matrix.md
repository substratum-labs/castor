# Live Rust `castord` S-Pay Fault Matrix

Generated at `2026-09-05T06:28:26.151648+00:00` on `macOS-26.5.2-arm64-arm-64bit` with binary SHA-256 `5dc73b387394b03049e41d179b217de1334cb1aef444e4949e814942b6b0e0db`. Every initial worker was physically terminated by its parent with SIGKILL and resumed against the same live physical `castord`. SQLite is the independent actuator truth.

Happy-path sentinel: 2 committed effects, 0 duplicates.

| System | Fault | Trials | Duplicate trials | Duplicate commits | Total committed effects |
|---|---|---:|---:|---:|---:|
| Castor Full | `kill_after_commit` | 20 | 0/20 | 0 | 40 |
| Castor Full | `kill_after_success` | 20 | 0/20 | 0 | 40 |
| No stable operation ID | `kill_after_commit` | 20 | 20/20 | 20 | 60 |
| No stable operation ID | `kill_after_success` | 20 | 0/20 | 0 | 40 |
| No actuator deduplication | `kill_after_commit` | 20 | 20/20 | 20 | 60 |
| No actuator deduplication | `kill_after_success` | 20 | 0/20 | 0 | 40 |
| Naive re-execution | `kill_after_commit` | 20 | 20/20 | 20 | 60 |
| Naive re-execution | `kill_after_success` | 20 | 20/20 | 40 | 80 |

`kill_after_commit` occurs after the payment SQLite transaction and durable dispatch record but before `DeliverArmedAttempt`. `kill_after_success` occurs after both action settlements are durable.
