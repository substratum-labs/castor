# TLC Model Checker Verification: Cognitive Recovery (EPIC-34 / T-314-A)

- **Date**: 2026-09-06
- **Spec**: `CognitiveRecovery.tla`
- **Config**: `CognitiveRecovery.cfg`
- **JDK**: OpenJDK 26.0.2.1 (`/opt/homebrew/Cellar/openjdk/26.0.2.1/bin/java`)
- **TLC Version**: `TLC2 Version 2026.09.04.170753 (rev: b123b22)` (`/Users/yong/.local/share/tla/tla2tools.jar`)
- **Status**: **PASS (0 Errors)**

---

## 1. Execution Command

```bash
/opt/homebrew/Cellar/openjdk/26.0.2.1/bin/java -XX:+UseParallelGC -cp /Users/yong/.local/share/tla/tla2tools.jar tlc2.TLC -workers 4 -config CognitiveRecovery.cfg CognitiveRecovery.tla
```

---

## 2. Configuration Parameters

```tla
SPECIFICATION Spec

CONSTANTS
    TurnIds = {1, 2}
    Actions = {1, 2}
    MaxProbeBudget = 2

INVARIANTS
    TypeOK
    S1_UnauthenticatedEvidenceRejected
    S2_UnknownBlocksConflictingMutations
    S3_ConfirmedAtMostOnce
    S4_NotAppliedExcludesOldDispatches
    S5_SettlementIdempotence
    S6_BudgetPreservedAcrossRestarts
    S7_StaleSnapshotGrantsNoAuthority
```

---

## 3. TLC Run Output

```text
TLC2 Version 2026.09.04.170753 (rev: b123b22)
Running breadth-first search Model-Checking with fp 48 and seed -2377900134847607825 with 4 workers on 12 cores with 5461MB heap and 64MB offheap memory [pid: 48327] (Mac OS X 26.5.2 aarch64, Homebrew 26.0.2.1 64bit, MSBDiskFPSet, DiskStateQueue).
Parsing file /Users/yong/projects/substratum/castor/specs/tla/CognitiveRecovery.tla
Parsing file /private/var/folders/vq/0lmt2t7j6lz37pfmbx50x5k80000gn/T/tlc-11966615138440206442/Naturals.tla (jar:file:/Users/yong/.local/share/tla/tla2tools.jar!/tla2sany/StandardModules/Naturals.tla)
Parsing file /private/var/folders/vq/0lmt2t7j6lz37pfmbx50x5k80000gn/T/tlc-11966615138440206442/FiniteSets.tla (jar:file:/Users/yong/.local/share/tla/tla2tools.jar!/tla2sany/StandardModules/FiniteSets.tla)
Parsing file /private/var/folders/vq/0lmt2t7j6lz37pfmbx50x5k80000gn/T/tlc-11966615138440206442/Sequences.tla (jar:file:/Users/yong/.local/share/tla/tla2tools.jar!/tla2sany/StandardModules/Sequences.tla)
Parsing file /private/var/folders/vq/0lmt2t7j6lz37pfmbx50x5k80000gn/T/tlc-11966615138440206442/TLC.tla (jar:file:/Users/yong/.local/share/tla/tla2tools.jar!/tla2sany/StandardModules/TLC.tla)
Parsing file /private/var/folders/vq/0lmt2t7j6lz37pfmbx50x5k80000gn/T/tlc-11966615138440206442/_TLCTrace.tla (jar:file:/Users/yong/.local/share/tla/tla2tools.jar!/tla2sany/StandardModules/_TLCTrace.tla)
Parsing file /private/var/folders/vq/0lmt2t7j6lz37pfmbx50x5k80000gn/T/tlc-11966615138440206442/TLCExt.tla (jar:file:/Users/yong/.local/share/tla/tla2tools.jar!/tla2sany/StandardModules/TLCExt.tla)
Parsing file /private/var/folders/vq/0lmt2t7j6lz37pfmbx50x5k80000gn/T/tlc-11966615138440206442/Integers.tla (jar:file:/Users/yong/.local/share/tla/tla2tools.jar!/tla2sany/StandardModules/Integers.tla)
Semantic processing of module Naturals
Semantic processing of module Sequences
Semantic processing of module FiniteSets
Semantic processing of module TLC
Semantic processing of module Integers
Semantic processing of module TLCExt
Semantic processing of module _TLCTrace
Semantic processing of module CognitiveRecovery
Linting of module TLCExt
Linting of module _TLCTrace
Linting of module CognitiveRecovery
Starting... (2026-09-06 01:49:06)
Computing initial states...
Finished computing initial states: 1 distinct state generated at 2026-09-06 01:49:06.
Model checking completed. No error has been found.
  Estimates of the probability that TLC did not check all reachable states
  because two distinct states had the same fingerprint:
  calculated (optimistic):  val = 4.7E-13
7777 states generated, 1360 distinct states found, 0 states left on queue.
The depth of the complete state graph search is 17.
The average outdegree of the complete state graph is 1 (minimum is 0, the maximum 6 and the 95th percentile is 3).
Finished in 00s at (2026-09-06 01:49:06)
```

---

## 4. Invariant Verification Analysis

1. **S1 (UnauthenticatedEvidenceRejected)**:
   - Evaluated across all 1,360 reachable states.
   - Proves no attempt transitions to `Confirmed` or `NotApplied` unless `authenticatedEvidence` has been minted by `EvidenceServiceVerifyAndMint`.
   - Guest attempts to forge settlement certificates (`GuestForgeSettlementAttempt`) stutter with 0 state change.

2. **S2 (UnknownBlocksConflictingMutations)**:
   - Whenever an attempt is in `ArmedUnknown`, `Dispatched`, or `QuarantinedDispute`, `scopeLocked["payment-scope"]` is guaranteed `TRUE`.
   - `PresentAdmission` strictly checks `~scopeLocked`, preventing any new mutating admissions during unsettled intervals.

3. **S3 (ConfirmedAtMostOnce)**:
   - For all actions in state `Confirmed`, `committedCount[a] <= 1`.
   - Re-execution or multiple executions are completely prevented across the decoupled network buffer.

4. **S4 (NotAppliedExcludesOldDispatches)**:
   - For all actions in state `NotApplied`, the physical actuator is in `TerminatedRejected` and `committedCount[a] = 0`.
   - Proves that late network packet arrivals (`ActuatorLateArrivalAtTombstone`) are dropped without physical execution.

5. **S5 (SettlementIdempotence)**:
   - Replaying an existing valid settlement (`IdempotentSettlementReplay`) maintains `lastSettlementDigest[a] <= 1` without duplicating journal state or projection changes.

6. **S6 (BudgetPreservedAcrossRestarts)**:
   - Daemon crashes and restarts (`RestartDaemon`) reconstruct `probeBudgetRemaining` from `journaledBudget`.
   - Reconstructed budget is provably bounded by `journaledBudget`.

7. **S7 (StaleSnapshotGrantsNoAuthority)**:
   - Stale snapshots or revoked capabilities (`RevokeCapability`) strictly prevent admissions (`StaleSnapshotAdmissionAttempt` rejected).
