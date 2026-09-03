//! Governed-turn composition contract for the single D1 Core journal.

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
        for (region, bytes) in [
            (
                "region://fixture",
                b"durably persisted governed-turn fixture".as_slice(),
            ),
            ("region://action-manifest", b"action-1\naction-2".as_slice()),
            (
                "region://action-manifest-2",
                b"second action manifest specification".as_slice(),
            ),
            (
                "region://conflicting-evidence",
                b"conflicting settlement evidence".as_slice(),
            ),
        ] {
            assert!(matches!(
                storage.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
                castor_kernel::c01_storage::EnsureRegionOutcome::Success(_)
            ));
        }
        Self {
            _root: root,
            authority: D1GovernedTurnAuthority::for_test(storage),
        }
    }

    fn ready_to_commit(&mut self) {
        assert_eq!(
            self.authority.admit_turn(admit()),
            GovernedTurnOutcome::Admitted
        );
        assert_eq!(
            self.authority.request_interaction(request_interaction()),
            GovernedTurnOutcome::InteractionRequested
        );
        assert_eq!(
            self.authority.report_outcome(report_interaction()),
            GovernedTurnOutcome::InteractionBound
        );
        assert_eq!(
            self.authority.consume_interaction(consume()),
            GovernedTurnOutcome::InteractionConsumed
        );
    }

    fn committed_action(&mut self) {
        self.ready_to_commit();
        assert_eq!(
            self.authority.commit_turn(commit()),
            GovernedTurnOutcome::TurnCommitted
        );
        assert_eq!(
            self.authority.register_action(ActionRegistrationRequest {
                action_id: "action-1".into()
            }),
            GovernedTurnOutcome::ActionRegistered
        );
    }

    fn armed_action(&mut self) {
        self.committed_action();
        assert_eq!(
            self.authority.present_admission_certificate(admission()),
            GovernedTurnOutcome::AttemptArmed { attempt_id: 1 }
        );
    }

    fn dispatched_action(&mut self) {
        self.armed_action();
        assert_eq!(
            self.authority.record_dispatch_attempt(dispatch()),
            GovernedTurnOutcome::DispatchRecorded
        );
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
        action_manifest_region_id: "region://action-manifest".into(),
        action_manifest_digest: digest(b"action-1\naction-2"),
        action_manifest: vec!["action-1".into(), "action-2".into()],
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
    c.dispatched_action();
    assert_eq!(
        c.authority
            .deliver_armed_attempt(DeliverArmedAttemptRequest {
                attempt_id: 1,
                dispatch_identity: "dispatch-1".into()
            }),
        GovernedTurnOutcome::Delivered
    );
    assert_eq!(
        c.authority.present_settlement_certificate(settlement()),
        GovernedTurnOutcome::Settled {
            resolution: "Confirmed".into()
        }
    );
}
#[test]
fn test_comp_commit_turn_while_awaiting_interaction_rejected() {
    let mut c = Fixture::new();
    assert_eq!(
        c.authority.admit_turn(admit()),
        GovernedTurnOutcome::Admitted
    );
    assert_eq!(
        c.authority.request_interaction(request_interaction()),
        GovernedTurnOutcome::InteractionRequested
    );
    assert_eq!(
        c.authority.commit_turn(commit()),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(
        c.authority.consume_interaction(consume()),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
}
#[test]
fn test_comp_unpersisted_observation_region_blocks_interaction_binding() {
    let mut c = Fixture::new();
    assert_eq!(
        c.authority.admit_turn(admit()),
        GovernedTurnOutcome::Admitted
    );
    assert_eq!(
        c.authority.request_interaction(request_interaction()),
        GovernedTurnOutcome::InteractionRequested
    );
    let mut report = report_interaction();
    report.observation_region_id = "region://missing".into();
    assert_eq!(
        c.authority.report_outcome(report),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
}
#[test]
fn test_comp_observation_consumed_only_under_strictly_monotonic_fresh_lease() {
    let mut c = Fixture::new();
    assert_eq!(
        c.authority.admit_turn(admit()),
        GovernedTurnOutcome::Admitted
    );
    assert_eq!(
        c.authority.request_interaction(request_interaction()),
        GovernedTurnOutcome::InteractionRequested
    );
    assert_eq!(
        c.authority.report_outcome(report_interaction()),
        GovernedTurnOutcome::InteractionBound
    );
    let mut stale = consume();
    stale.lease_epoch = 0;
    assert_eq!(
        c.authority.consume_interaction(stale),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(
        c.authority.consume_interaction(consume()),
        GovernedTurnOutcome::InteractionConsumed
    );
}
#[test]
fn test_comp_unpersisted_successor_or_action_region_blocks_turn_commit() {
    let mut c = Fixture::new();
    c.ready_to_commit();
    let mut missing_successor = commit();
    missing_successor.successor_region_id = "region://missing".into();
    assert_eq!(
        c.authority.commit_turn(missing_successor),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
    let mut missing_manifest = commit();
    missing_manifest.action_manifest_region_id = "region://missing".into();
    assert_eq!(
        c.authority.commit_turn(missing_manifest),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
}
#[test]
fn test_comp_fence_prevents_stale_carrier_commit_and_action_publication() {
    let mut c = Fixture::new();
    c.ready_to_commit();
    assert_eq!(
        c.authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    assert_eq!(
        c.authority.commit_turn(commit()),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(
        c.authority.register_action(ActionRegistrationRequest {
            action_id: "action-1".into()
        }),
        GovernedTurnOutcome::RejectedPrecondition
    );
}
#[test]
fn test_comp_arm_attempt_before_turn_commit_rejected() {
    let mut c = Fixture::new();
    assert_eq!(
        c.authority.present_admission_certificate(admission()),
        GovernedTurnOutcome::RejectedPrecondition
    );
}
#[test]
fn test_comp_admission_rejected_when_generation_fenced_or_capability_revoked() {
    let mut c = Fixture::new();
    c.committed_action();
    assert_eq!(
        c.authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    assert_eq!(
        c.authority.present_admission_certificate(admission()),
        GovernedTurnOutcome::RejectedStaleGeneration {
            current_generation: 2
        }
    );
    let mut c = Fixture::new();
    c.committed_action();
    assert_eq!(
        c.authority.revoke_capability("capability-1"),
        GovernedTurnOutcome::CapabilityRevoked
    );
    assert_eq!(
        c.authority.present_admission_certificate(admission()),
        GovernedTurnOutcome::RejectedCapabilityRevoked
    );
}
#[test]
fn test_comp_armed_unknown_blocks_overlapping_action_scope_admission() {
    let mut c = Fixture::new();
    c.armed_action();
    assert_eq!(
        c.authority.register_action(ActionRegistrationRequest {
            action_id: "action-2".into()
        }),
        GovernedTurnOutcome::ActionRegistered
    );
    let mut other = admission();
    other.action_id = "action-2".into();
    assert_eq!(
        c.authority.present_admission_certificate(other),
        GovernedTurnOutcome::RejectedCurrentState
    );
}
#[test]
fn test_comp_crash_after_arm_reopens_storage_reconstructs_unknown_and_rejects_retry() {
    let mut c = Fixture::new();
    c.armed_action();
    assert_eq!(
        c.authority.reconstruct_after_crash(),
        GovernedTurnOutcome::Reconstructed
    );
    assert_eq!(
        c.authority.present_admission_certificate(admission()),
        GovernedTurnOutcome::RejectedCurrentState
    );
}
#[test]
fn test_comp_duplicate_deliver_armed_attempt_calls_provider_only_once() {
    let mut c = Fixture::new();
    c.dispatched_action();
    let request = DeliverArmedAttemptRequest {
        attempt_id: 1,
        dispatch_identity: "dispatch-1".into(),
    };
    assert_eq!(
        c.authority.deliver_armed_attempt(request.clone()),
        GovernedTurnOutcome::Delivered
    );
    assert_eq!(
        c.authority.deliver_armed_attempt(request),
        GovernedTurnOutcome::DuplicateDelivery
    );
    assert_eq!(c.authority.provider_submission_count(), 1);
}
#[test]
fn test_comp_crash_after_dispatch_with_missing_dedup_fails_closed_as_ambiguous() {
    let mut c = Fixture::new();
    c.dispatched_action();
    assert_eq!(
        c.authority.reconstruct_after_crash(),
        GovernedTurnOutcome::Reconstructed
    );
    assert_eq!(
        c.authority
            .deliver_armed_attempt(DeliverArmedAttemptRequest {
                attempt_id: 1,
                dispatch_identity: "dispatch-1".into()
            }),
        GovernedTurnOutcome::Ambiguous
    );
}
#[test]
fn test_comp_confirmed_settlement_requires_exact_dispatch_identity() {
    let mut c = Fixture::new();
    c.dispatched_action();
    let mut certificate = settlement();
    certificate.dispatch_identity = "unmatched-dispatch".into();
    assert_eq!(
        c.authority.present_settlement_certificate(certificate),
        GovernedTurnOutcome::RejectedCurrentState
    );
}
#[test]
fn test_comp_unpersisted_evidence_region_blocks_settlement() {
    let mut c = Fixture::new();
    c.dispatched_action();
    let mut certificate = settlement();
    certificate.evidence_region_id = "region://missing".into();
    assert_eq!(
        c.authority.present_settlement_certificate(certificate),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
}
#[test]
fn test_comp_weak_timeout_rejected_and_preserves_armed_unknown_blocking_mutex() {
    let mut c = Fixture::new();
    c.dispatched_action();
    let mut certificate = settlement();
    certificate.resolution = "NotApplied".into();
    certificate.proof_class = "TimeoutTelemetry".into();
    assert_eq!(
        c.authority.present_settlement_certificate(certificate),
        GovernedTurnOutcome::RejectedInvalidProofClass
    );
    assert_eq!(
        c.authority.register_action(ActionRegistrationRequest {
            action_id: "action-2".into()
        }),
        GovernedTurnOutcome::ActionRegistered
    );
    let mut other = admission();
    other.action_id = "action-2".into();
    assert_eq!(
        c.authority.present_admission_certificate(other),
        GovernedTurnOutcome::RejectedCurrentState
    );
}
#[test]
fn test_comp_pre_dispatch_not_applied_with_verifiable_non_execution_settles_and_unlocks_scope() {
    let mut c = Fixture::new();
    c.armed_action();
    let mut certificate = settlement();
    certificate.resolution = "NotApplied".into();
    certificate.proof_class = "VerifiableNonExecution".into();
    certificate.dispatch_identity.clear();
    assert_eq!(
        c.authority.present_settlement_certificate(certificate),
        GovernedTurnOutcome::Settled {
            resolution: "NotApplied".into()
        }
    );
    assert_eq!(
        c.authority.register_action(ActionRegistrationRequest {
            action_id: "action-2".into()
        }),
        GovernedTurnOutcome::ActionRegistered
    );
    let mut other = admission();
    other.action_id = "action-2".into();
    assert_eq!(
        c.authority.present_admission_certificate(other),
        GovernedTurnOutcome::AttemptArmed { attempt_id: 2 }
    );
}
#[test]
fn test_comp_late_interaction_after_turn_close_does_not_bind_successor_turn() {
    let mut c = Fixture::new();
    c.ready_to_commit();
    assert_eq!(
        c.authority.commit_turn(commit()),
        GovernedTurnOutcome::TurnCommitted
    );
    assert_eq!(
        c.authority.admit_turn(AdmitTurnRequest {
            turn_id: 2,
            base_projection_digest: digest(b"durably persisted governed-turn fixture"),
            ..admit()
        }),
        GovernedTurnOutcome::Admitted
    );
    assert_eq!(
        c.authority.report_outcome(report_interaction()),
        GovernedTurnOutcome::RejectedLateOrClosedTurn
    );
}
#[test]
fn test_comp_quarantined_dispute_blocks_different_action_on_same_scope() {
    let mut c = Fixture::new();
    c.dispatched_action();
    assert_eq!(
        c.authority.present_settlement_certificate(settlement()),
        GovernedTurnOutcome::Settled {
            resolution: "Confirmed".into()
        }
    );
    let mut conflicting = settlement();
    conflicting.evidence_region_id = "region://conflicting-evidence".into();
    conflicting.evidence_digest = digest(b"conflicting settlement evidence");
    assert_eq!(
        c.authority.present_settlement_certificate(conflicting),
        GovernedTurnOutcome::QuarantinedDispute
    );
    assert_eq!(
        c.authority.register_action(ActionRegistrationRequest {
            action_id: "action-2".into()
        }),
        GovernedTurnOutcome::ActionRegistered
    );
    let mut other = admission();
    other.action_id = "action-2".into();
    assert_eq!(
        c.authority.present_admission_certificate(other),
        GovernedTurnOutcome::RejectedCurrentState
    );
}
