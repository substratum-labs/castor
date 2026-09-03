//! RED hostile-contract tests for the EPIC-24 C-05 D1 admission and settlement slice.
//!
//! These tests state the frozen L5 semantics before the Phase-3 implementation
//! exists.  The temporary C-05 boundary deliberately fails closed, so this
//! suite is expected to fail after it compiles.

use castor_kernel::c05_settlement::{
    AdmissionCertificate, AdmissionRequest, AdmissionSettlement, AttemptStatus, D1Settlement,
    EvidenceBundle, ProofClass, ResolutionClass, SettlementCertificate, SettlementOutcome,
    SettlementRequest,
};

const AGENT_ID: &str = "agent-c05";
const ACTION_ID: &str = "action-payment-42";
const ACTION_DIGEST: &str = "sha256:immutable-action";
const TARGET_SCOPE: &str = "merchant:42";
const CAPABILITY_ID: &str = "capability:pay";
const DISPATCH_IDENTITY: &str = "sha256:dispatch-identity";
const EVIDENCE_DIGEST: &str = "sha256:evidence-bundle";
const GENERATION: u64 = 7;

fn harness() -> D1Settlement {
    D1Settlement::for_test(AGENT_ID, GENERATION)
}

fn admission(entry_id: u64) -> AdmissionRequest {
    AdmissionRequest {
        agent_id: AGENT_ID.into(),
        action_id: ACTION_ID.into(),
        action_digest: ACTION_DIGEST.into(),
        target_scope: TARGET_SCOPE.into(),
        capability_id: CAPABILITY_ID.into(),
        entry_id,
        certificate: AdmissionCertificate {
            action_id: ACTION_ID.into(),
            action_digest: ACTION_DIGEST.into(),
            generation: GENERATION,
            target_scope: TARGET_SCOPE.into(),
        },
    }
}

fn confirmed(attempt_id: u64, certificate_id: &str) -> SettlementCertificate {
    SettlementCertificate {
        certificate_id: certificate_id.into(),
        attempt_id,
        dispatch_identity: DISPATCH_IDENTITY.into(),
        evidence_bundle_digest: EVIDENCE_DIGEST.into(),
        proposed_resolution: ResolutionClass::Confirmed,
        proof_class: ProofClass::ProviderConfirmation,
    }
}

fn not_applied(
    attempt_id: u64,
    certificate_id: &str,
    proof_class: ProofClass,
) -> SettlementCertificate {
    SettlementCertificate {
        certificate_id: certificate_id.into(),
        attempt_id,
        dispatch_identity: DISPATCH_IDENTITY.into(),
        evidence_bundle_digest: EVIDENCE_DIGEST.into(),
        proposed_resolution: ResolutionClass::NotApplied,
        proof_class,
    }
}

fn arm(core: &mut D1Settlement, entry_id: u64) -> u64 {
    match core.present_admission_certificate(admission(entry_id)) {
        SettlementOutcome::AttemptArmedAck { attempt_id, .. } => attempt_id,
        other => panic!("valid admission must arm an attempt, got {other:?}"),
    }
}

fn record_dispatch(core: &mut D1Settlement, attempt_id: u64, entry_id: u64) {
    assert_eq!(
        core.record_dispatch_attempt(attempt_id, DISPATCH_IDENTITY, entry_id),
        SettlementOutcome::DispatchRecordedAck { entry_id },
    );
}

fn persist_evidence(core: &mut D1Settlement) {
    assert_eq!(
        core.persist_evidence(EvidenceBundle {
            region_id: "region://evidence/42".into(),
            digest: EVIDENCE_DIGEST.into(),
        }),
        SettlementOutcome::EvidencePersisted,
    );
}

fn settle(
    core: &mut D1Settlement,
    certificate: SettlementCertificate,
    entry_id: u64,
) -> SettlementOutcome {
    core.present_settlement_certificate(SettlementRequest {
        agent_id: AGENT_ID.into(),
        evidence_region_id: "region://evidence/42".into(),
        entry_id,
        certificate,
    })
}

#[test]
fn test_c05_normal_admission_arm_dispatch_settle_flow() {
    let mut core = harness();
    let attempt_id = arm(&mut core, 10);
    record_dispatch(&mut core, attempt_id, 11);
    persist_evidence(&mut core);
    assert_eq!(
        settle(&mut core, confirmed(attempt_id, "settlement-1"), 12),
        SettlementOutcome::AcceptedAndAppended {
            entry_id: 12,
            resolution: ResolutionClass::Confirmed,
        }
    );
}

#[test]
fn test_c05_crash_after_arm_reconstructs_unknown_not_retryable() {
    let mut core = harness();
    let _attempt_id = arm(&mut core, 20);
    core.reconstruct_after_crash();
    assert_eq!(
        core.attempt_status(ACTION_ID),
        Some(AttemptStatus::ArmedUnknown)
    );
    assert_eq!(
        core.present_admission_certificate(admission(21)),
        SettlementOutcome::RejectedCurrentState
    );
}

#[test]
fn test_c05_lost_ack_arm_does_not_mint_attempt_2() {
    let mut core = harness();
    let first_attempt = arm(&mut core, 30);
    assert_eq!(
        core.present_admission_certificate(admission(30)),
        SettlementOutcome::AttemptArmedAck {
            attempt_id: first_attempt,
            entry_id: 30,
        }
    );
}

#[test]
fn test_c05_unknown_blocks_overlapping_action_admission() {
    let mut core = harness();
    let _attempt_id = arm(&mut core, 40);
    let mut overlap = admission(41);
    overlap.action_id = "action-payment-43".into();
    overlap.certificate.action_id = overlap.action_id.clone();
    assert_eq!(
        core.present_admission_certificate(overlap),
        SettlementOutcome::RejectedCurrentState
    );
}

#[test]
fn test_c05_stale_generation_or_fenced_admission_rejected() {
    let mut core = harness();
    core.fence_generation(GENERATION + 1);
    assert_eq!(
        core.present_admission_certificate(admission(50)),
        SettlementOutcome::RejectedStaleGeneration {
            current_generation: GENERATION + 1,
        }
    );
}

#[test]
fn test_c05_capability_revoked_admission_rejected() {
    let mut core = harness();
    core.revoke_capability(CAPABILITY_ID);
    assert_eq!(
        core.present_admission_certificate(admission(60)),
        SettlementOutcome::RejectedCapabilityRevoked
    );
}

#[test]
fn test_c05_confirmed_without_dispatch_identity_rejected() {
    let mut core = harness();
    let attempt_id = arm(&mut core, 70);
    persist_evidence(&mut core);
    assert_eq!(
        settle(&mut core, confirmed(attempt_id, "settlement-7"), 71),
        SettlementOutcome::RejectedCurrentState
    );
}

#[test]
fn test_c05_weak_not_applied_rejected_preserves_unknown() {
    let mut core = harness();
    let attempt_id = arm(&mut core, 80);
    persist_evidence(&mut core);
    assert_eq!(
        settle(
            &mut core,
            not_applied(attempt_id, "settlement-8", ProofClass::TimeoutTelemetry),
            81,
        ),
        SettlementOutcome::RejectedInvalidProofClass
    );
    assert_eq!(
        core.attempt_status(ACTION_ID),
        Some(AttemptStatus::ArmedUnknown)
    );
}

#[test]
fn test_c05_valid_not_applied_then_confirmed_is_quarantined_dispute() {
    let mut core = harness();
    let attempt_id = arm(&mut core, 90);
    record_dispatch(&mut core, attempt_id, 91);
    persist_evidence(&mut core);
    assert_eq!(
        settle(
            &mut core,
            not_applied(
                attempt_id,
                "settlement-9a",
                ProofClass::VerifiableNonExecution,
            ),
            92,
        ),
        SettlementOutcome::AcceptedAndAppended {
            entry_id: 92,
            resolution: ResolutionClass::NotApplied,
        }
    );
    assert_eq!(
        settle(&mut core, confirmed(attempt_id, "settlement-9b"), 93),
        SettlementOutcome::ConflictingEvidenceAppended
    );
    assert_eq!(
        core.attempt_status(ACTION_ID),
        Some(AttemptStatus::QuarantinedDispute)
    );
}

#[test]
fn test_c05_duplicate_certificate_idempotence() {
    let mut core = harness();
    let attempt_id = arm(&mut core, 100);
    record_dispatch(&mut core, attempt_id, 101);
    persist_evidence(&mut core);
    let certificate = confirmed(attempt_id, "settlement-10");
    assert!(matches!(
        settle(&mut core, certificate.clone(), 102),
        SettlementOutcome::AcceptedAndAppended { .. }
    ));
    assert_eq!(
        settle(&mut core, certificate, 103),
        SettlementOutcome::AlreadyAppendedSameCertificate { entry_id: 102 }
    );
}

#[test]
fn test_c05_settlement_unpersisted_evidence_region_rejected() {
    let mut core = harness();
    let attempt_id = arm(&mut core, 110);
    record_dispatch(&mut core, attempt_id, 111);
    assert_eq!(
        settle(&mut core, confirmed(attempt_id, "settlement-11"), 112),
        SettlementOutcome::IntegrityOrProtocolFault
    );
}

#[test]
fn test_c05_roche_unknown_blocks_new_arm_not_existing() {
    let mut core = harness();
    let attempt_id = arm(&mut core, 120);
    record_dispatch(&mut core, attempt_id, 121);
    persist_evidence(&mut core);
    core.set_roche_isolation_unknown();
    assert_eq!(
        core.present_admission_certificate(admission(122)),
        SettlementOutcome::RejectedCurrentState
    );
    assert!(matches!(
        settle(&mut core, confirmed(attempt_id, "settlement-12"), 123),
        SettlementOutcome::AcceptedAndAppended { .. }
    ));
}
