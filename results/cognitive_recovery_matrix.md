# Physical Cognitive Recovery Fault-Injection Matrix (T-314-D)

- **Date / Time (UTC)**: `2026-09-08T05:28:23.250937+00:00`
- **Platform**: `macOS-26.5.2-arm64-arm-64bit-Mach-O` (`arm64`)
- **Target Binary**: `/Users/yong/projects/substratum/castor/.worktrees/t-314-d-finalize/kernel/target/release/castord`
- **Binary SHA-256**: `1d5d42575fb3a86a0db99714b8778667aa2133ec0e3d1fecd8e69c7bb32f00fa`
- **Trials per Cell**: `20`
- **Total Trials**: `280` (`160` adaptive + `120` comparative)

## 1. Fault-Injection Cell Matrix (Adaptive Cognitive Recovery)

| Fault Seam | Crash Target | Trials | Duplicate Effects | Recovery Rate | Latency p50 (ms) | Latency p95 (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `arm` | `daemon_sigkill` | 20 | **0** | 100.0% | 136.63 | 140.33 |
| `arm` | `worker_sigkill` | 20 | **0** | 100.0% | 124.54 | 133.62 |
| `t1_dispatch_committed` | `daemon_sigkill` | 20 | **0** | 100.0% | 168.65 | 179.07 |
| `t1_dispatch_committed` | `worker_sigkill` | 20 | **0** | 100.0% | 151.39 | 162.24 |
| `t2_dispatch_late_arrival` | `daemon_sigkill` | 20 | **0** | 100.0% | 169.54 | 186.63 |
| `t2_dispatch_late_arrival` | `worker_sigkill` | 20 | **0** | 100.0% | 142.74 | 148.54 |
| `torn_journal_tail` | `daemon_sigkill` | 20 | **0** | 100.0% | 169.5 | 178.53 |
| `t1_worker_commit` | `worker_sigkill` | 20 | **0** | 100.0% | 140.75 | 149.64 |

## 2. Comparative Policy Evaluation

| Recovery Policy | Total Trials | Duplicate Effects | Success Rate | Avg Probes / Trial | HITL Request Proxy Rate | Latency p50 (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **adaptive_cognitive** | 160 | **0** | 100.0% | 0.75 | 0.0% | 146.66 |
| **direct_escalation** | 40 | **0** | 0.0% | 0.0 | 100.0% | 98.17 |
| **fixed_query** | 40 | **0** | 50.0% | 1.0 | 50.0% | 127.46 |
| **blind_retry** | 40 | **0** | 0.0% | 0.0 | 0.0% | 78.24 |

## 3. Ground Truth Verification Invariants

1. **Zero Duplicate Effects ($E = 0$)**: Across all 160 physical fault injection trials (4 seams $\times$ 2 crash targets $\times$ 20 trials), Castor's adaptive cognitive recovery produced exactly 0 duplicate commits on the external SQLite actuator.
2. **Physical Subprocess Termination**: Every trial verified real OS signal delivery: carrier processes received `SIGKILL` with asserted `-SIGKILL` exit status while daemon remained alive, or release daemon received `SIGKILL` and was restarted with a distinct PID.
3. **Physical Commit Persistence (T1 & T2)**: In `t1_dispatch_committed`, actuator committed to SQLite before crash; recovery verified `Committed` status and settled without secondary execution. In `t2_dispatch_late_arrival`, ambiguity was preserved while write-locked until delayed arrival committed.
4. **Unified Objective Predicate**: Recovery was verified via an objective kernel predicate (`locked_scopes == 0` in Core projection and actuator `commits == 1`), eliminating author-selected booleans.
5. **Cell Semantics**: `torn_journal_tail` is the daemon-crash cell that injects and truncates an incomplete journal frame. `t1_worker_commit` kills the carrier after an actuator commit while the daemon remains live; it is a T1 recovery cell and does not claim torn-tail coverage.
6. **HITL Metric Boundary**: `HITL Request Proxy Rate` counts harness requests for operator attention. `direct_escalation` does not drive the kernel into `Escalated`; the control-only `SubmitDecision` boundary is verified separately by R10.
7. **Recovery Driver Boundary**: The parent matrix runner performs the recovery decisions. It exercises Core-authored snapshots and the evidence channel but does not execute `examples/cognitive_recovery_agent.py` as an independently resumed Ring-3 worker.
