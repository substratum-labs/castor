# Physical Cognitive Recovery Fault-Injection Matrix (T-314-D)

- **Date / Time (UTC)**: `2026-09-07T06:28:33.356822+00:00`
- **Platform**: `macOS-26.5.2-arm64-arm-64bit-Mach-O` (`arm64`)
- **Target Binary**: `/Users/yong/projects/substratum/castor/kernel/target/release/castord`
- **Binary SHA-256**: `6b8e3757fcf0e09290f879fae38378396a432eec378e6f6ab25da690f85cc0ba`
- **Trials per Cell**: `20`

## 1. Fault-Injection Cell Matrix (Adaptive Cognitive Recovery)

| Fault Seam | Crash Target | Trials | Duplicate Effects | Recovery Rate | Latency p50 (ms) | Latency p95 (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `arm` | `daemon_sigkill` | 20 | **0** | 100.0% | 142.63 | 348.04 |
| `arm` | `worker_sigkill` | 20 | **0** | 100.0% | 126.03 | 152.96 |
| `t1_dispatch_committed` | `daemon_sigkill` | 20 | **0** | 100.0% | 154.85 | 180.93 |
| `t1_dispatch_committed` | `worker_sigkill` | 20 | **0** | 100.0% | 140.97 | 237.09 |
| `t2_dispatch_late_arrival` | `daemon_sigkill` | 20 | **0** | 100.0% | 164.7 | 182.04 |
| `t2_dispatch_late_arrival` | `worker_sigkill` | 20 | **0** | 100.0% | 143.1 | 159.23 |
| `torn-settlement` | `daemon_sigkill` | 20 | **0** | 100.0% | 163.11 | 181.78 |
| `torn-settlement` | `worker_sigkill` | 20 | **0** | 100.0% | 139.02 | 163.59 |

## 2. Comparative Policy Evaluation

| Recovery Policy | Total Trials | Duplicate Effects | Success Rate | Avg Probes / Trial | HITL Escalation Rate | Latency p50 (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **adaptive_cognitive** | 160 | **0** | 100.0% | 0.75 | 0.0% | 147.06 |
| **direct_escalation** | 40 | **0** | 0.0% | 0.0 | 100.0% | 92.74 |
| **fixed_query** | 40 | **0** | 50.0% | 1.0 | 50.0% | 129.96 |
| **blind_retry** | 40 | **0** | 0.0% | 0.0 | 0.0% | 76.49 |

## 3. Ground Truth Verification Invariants

1. **Zero Duplicate Effects ($E = 0$)**: Across all 160 physical fault injection trials (4 seams $	imes$ 2 crash targets $	imes$ 20 trials), Castor's adaptive cognitive recovery produced exactly 0 duplicate commits on the external SQLite actuator.
2. **Physical Subprocess Termination**: Every trial verified real OS signal delivery: carrier processes received `SIGKILL` with asserted `-SIGKILL` exit status while daemon remained alive, or release daemon received `SIGKILL` and was restarted with a distinct PID.
3. **Physical Commit Persistence (T1 & T2)**: In `t1_dispatch_committed`, actuator committed to SQLite before crash; recovery verified `Committed` status and settled without secondary execution. In `t2_dispatch_late_arrival`, ambiguity was preserved while write-locked until delayed arrival committed.
4. **Unified Objective Predicate**: Recovery was verified via an objective kernel predicate (`locked_scopes == 0` in Core projection and actuator `commits == 1`), eliminating author-selected booleans.
