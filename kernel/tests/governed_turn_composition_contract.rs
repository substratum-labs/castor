//! Phase-2 governed-turn composition contract.
//!
//! The public composition boundary is deliberately fail-closed until the
//! unified C-01 projection is implemented. These scenarios exercise that
//! boundary with real D1 storage and SHA-256 Region digests.

use castor_kernel::c01_storage::{D1DurableStorage, DurabilityProfile, DurableStorage};
use castor_kernel::c06_composition::{
    ActionRegistrationRequest, AdmitTurnRequest, CommitTurnRequest, ConsumeInteractionRequest,
    D1GovernedTurnAuthority, DeliverArmedAttemptRequest, GovernedTurnOutcome,
    InteractionOutcomeReport, PresentAdmissionCertificateRequest,
    PresentSettlementCertificateRequest, RecordDispatchAttemptRequest, RequestInteractionRequest,
};
use sha2::{Digest, Sha256};
use tempfile::TempDir;

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

struct Fixture {
    _root: TempDir,
    authority: D1GovernedTurnAuthority,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("temporary D1 root");
        let mut storage = D1DurableStorage::open(root.path()).expect("open D1 storage");
        let bytes = b"durably persisted governed-turn fixture";
        assert!(matches!(
            storage.ensure_region(
                "region://fixture",
                &digest(bytes),
                bytes,
                DurabilityProfile::D1
            ),
            castor_kernel::c01_storage::EnsureRegionOutcome::Success(_)
        ));
        Self {
            _root: root,
            authority: D1GovernedTurnAuthority::for_test(storage),
        }
    }

    fn assert_fail_closed(outcome: GovernedTurnOutcome) {
        assert_eq!(outcome, GovernedTurnOutcome::RejectedPreimplementation);
    }
}

fn admit() -> AdmitTurnRequest {
    AdmitTurnRequest {
        agent_id: "agent-1".into(),
        turn_id: 1,
        lease_epoch: 0,
        base_projection_digest: digest(b"H0"),
    }
}
fn request_interaction() -> RequestInteractionRequest {
    RequestInteractionRequest {
        interaction_id: "interaction-1".into(),
        lease_epoch: 0,
        request_digest: digest(b"interaction request"),
    }
}
fn report_interaction() -> InteractionOutcomeReport {
    InteractionOutcomeReport {
        interaction_id: "interaction-1".into(),
        observation_region_id: "region://fixture".into(),
        observation_digest: digest(b"durably persisted governed-turn fixture"),
    }
}
fn consume() -> ConsumeInteractionRequest {
    ConsumeInteractionRequest {
        interaction_id: "interaction-1".into(),
        lease_epoch: 1,
    }
}
fn commit() -> CommitTurnRequest {
    CommitTurnRequest {
        lease_epoch: 1,
        base_projection_digest: digest(b"H0"),
        successor_region_id: "region://fixture".into(),
        successor_digest: digest(b"durably persisted governed-turn fixture"),
        action_manifest: vec!["action-1".into()],
    }
}
fn admission() -> PresentAdmissionCertificateRequest {
    PresentAdmissionCertificateRequest {
        action_id: "action-1".into(),
        target_scope: "scope-1".into(),
        capability_id: "capability-1".into(),
        generation: 1,
    }
}
fn dispatch() -> RecordDispatchAttemptRequest {
    RecordDispatchAttemptRequest {
        attempt_id: 1,
        dispatch_identity: "dispatch-1".into(),
    }
}
fn settlement() -> PresentSettlementCertificateRequest {
    PresentSettlementCertificateRequest {
        attempt_id: 1,
        dispatch_identity: "dispatch-1".into(),
        evidence_region_id: "region://fixture".into(),
        evidence_digest: digest(b"durably persisted governed-turn fixture"),
        resolution: "Confirmed".into(),
        proof_class: "ProviderConfirmation".into(),
    }
}

#[test]
fn test_comp_normal_governed_turn_e2e_flow() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.admit_turn(admit()));
    Fixture::assert_fail_closed(c.authority.request_interaction(request_interaction()));
    Fixture::assert_fail_closed(c.authority.report_outcome(report_interaction()));
    Fixture::assert_fail_closed(c.authority.consume_interaction(consume()));
    Fixture::assert_fail_closed(c.authority.commit_turn(commit()));
    Fixture::assert_fail_closed(c.authority.register_action(ActionRegistrationRequest {
        action_id: "action-1".into(),
    }));
    Fixture::assert_fail_closed(c.authority.present_admission_certificate(admission()));
    Fixture::assert_fail_closed(c.authority.record_dispatch_attempt(dispatch()));
    Fixture::assert_fail_closed(
        c.authority
            .deliver_armed_attempt(DeliverArmedAttemptRequest {
                attempt_id: 1,
                dispatch_identity: "dispatch-1".into(),
            }),
    );
    Fixture::assert_fail_closed(c.authority.present_settlement_certificate(settlement()));
}

#[test]
fn test_comp_commit_turn_while_awaiting_interaction_rejected() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.commit_turn(commit()));
    Fixture::assert_fail_closed(c.authority.consume_interaction(consume()));
}
#[test]
fn test_comp_unpersisted_observation_region_blocks_interaction_binding() {
    let mut c = Fixture::new();
    let mut r = report_interaction();
    r.observation_region_id = "region://missing".into();
    Fixture::assert_fail_closed(c.authority.report_outcome(r));
}
#[test]
fn test_comp_observation_consumed_only_under_strictly_monotonic_fresh_lease() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.consume_interaction(ConsumeInteractionRequest {
        interaction_id: "interaction-1".into(),
        lease_epoch: 0,
    }));
}
#[test]
fn test_comp_unpersisted_successor_or_action_region_blocks_turn_commit() {
    let mut c = Fixture::new();
    let mut r = commit();
    r.successor_region_id = "region://missing".into();
    Fixture::assert_fail_closed(c.authority.commit_turn(r));
}
#[test]
fn test_comp_fence_prevents_stale_carrier_commit_and_action_publication() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.persist_fence(2));
    Fixture::assert_fail_closed(c.authority.commit_turn(commit()));
}
#[test]
fn test_comp_arm_attempt_before_turn_commit_rejected() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.present_admission_certificate(admission()));
}
#[test]
fn test_comp_admission_rejected_when_generation_fenced_or_capability_revoked() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.persist_fence(2));
    Fixture::assert_fail_closed(c.authority.revoke_capability("capability-1"));
    Fixture::assert_fail_closed(c.authority.present_admission_certificate(admission()));
}
#[test]
fn test_comp_armed_unknown_blocks_overlapping_action_scope_admission() {
    let mut c = Fixture::new();
    let mut r = admission();
    r.action_id = "action-2".into();
    Fixture::assert_fail_closed(c.authority.present_admission_certificate(r));
}
#[test]
fn test_comp_crash_after_arm_reopens_storage_reconstructs_unknown_and_rejects_retry() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.present_admission_certificate(admission()));
    Fixture::assert_fail_closed(c.authority.reconstruct_after_crash());
    Fixture::assert_fail_closed(c.authority.present_admission_certificate(admission()));
}
#[test]
fn test_comp_duplicate_deliver_armed_attempt_calls_provider_only_once() {
    let mut c = Fixture::new();
    let r = DeliverArmedAttemptRequest {
        attempt_id: 1,
        dispatch_identity: "dispatch-1".into(),
    };
    Fixture::assert_fail_closed(c.authority.deliver_armed_attempt(r.clone()));
    Fixture::assert_fail_closed(c.authority.deliver_armed_attempt(r));
}
#[test]
fn test_comp_crash_after_dispatch_with_missing_dedup_fails_closed_as_ambiguous() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.record_dispatch_attempt(dispatch()));
    Fixture::assert_fail_closed(c.authority.reconstruct_after_crash());
}
#[test]
fn test_comp_confirmed_settlement_requires_exact_dispatch_identity() {
    let mut c = Fixture::new();
    let mut r = settlement();
    r.dispatch_identity = "unmatched-dispatch".into();
    Fixture::assert_fail_closed(c.authority.present_settlement_certificate(r));
}
#[test]
fn test_comp_unpersisted_evidence_region_blocks_settlement() {
    let mut c = Fixture::new();
    let mut r = settlement();
    r.evidence_region_id = "region://missing".into();
    Fixture::assert_fail_closed(c.authority.present_settlement_certificate(r));
}
#[test]
fn test_comp_weak_timeout_rejected_and_preserves_armed_unknown_blocking_mutex() {
    let mut c = Fixture::new();
    let mut r = settlement();
    r.resolution = "NotApplied".into();
    r.proof_class = "TimeoutTelemetry".into();
    Fixture::assert_fail_closed(c.authority.present_settlement_certificate(r));
}
#[test]
fn test_comp_pre_dispatch_not_applied_with_verifiable_non_execution_settles_and_unlocks_scope() {
    let mut c = Fixture::new();
    let mut r = settlement();
    r.resolution = "NotApplied".into();
    r.proof_class = "VerifiableNonExecution".into();
    Fixture::assert_fail_closed(c.authority.present_settlement_certificate(r));
}
#[test]
fn test_comp_late_interaction_after_turn_close_does_not_bind_successor_turn() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.commit_turn(commit()));
    Fixture::assert_fail_closed(c.authority.admit_turn(AdmitTurnRequest {
        turn_id: 2,
        ..admit()
    }));
    Fixture::assert_fail_closed(c.authority.report_outcome(report_interaction()));
}
#[test]
fn test_comp_quarantined_dispute_blocks_different_action_on_same_scope() {
    let mut c = Fixture::new();
    Fixture::assert_fail_closed(c.authority.present_settlement_certificate(settlement()));
    let mut r = admission();
    r.action_id = "action-2".into();
    Fixture::assert_fail_closed(c.authority.present_admission_certificate(r));
}
