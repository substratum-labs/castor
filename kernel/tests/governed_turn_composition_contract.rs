//! Governed-turn composition contract for the single D1 Core journal.

use castor_kernel::c01_storage::{D1DurableStorage, DurabilityProfile, DurableStorage};
use castor_kernel::c06_composition::{
    ActionRegistrationRequest, AdmitTurnRequest, CapabilityGrant, CapabilityRight,
    CommitTurnRequest, ConsumeInteractionRequest, D1GovernedTurnAuthority,
    DeliverArmedAttemptRequest, DisputeResolution, GovernedTurnOutcome, GrantCapabilityRequest,
    InteractionOutcomeReport, PresentAdmissionCertificateRequest,
    PresentSettlementCertificateRequest, RecordDispatchAttemptRequest, RequestInteractionRequest,
    ResolveQuarantinedDisputeRequest,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
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
        let mut authority = D1GovernedTurnAuthority::for_test(storage);
        assert_eq!(
            authority.grant_capability(GrantCapabilityRequest {
                grant: CapabilityGrant {
                    cap_id: "capability-1".into(),
                    subject: "agent-1".into(),
                    object_ref: "c04:generic".into(),
                    rights: vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
                    constraints: vec![],
                    parent_cap_id: None,
                    revocation_domain: None,
                    delegation_allowed: false,
                    max_turns: None,
                }
            }),
            GovernedTurnOutcome::CapabilityGranted
        );
        Self {
            _root: root,
            authority,
        }
    }

    fn ready_to_commit(&mut self) {
        assert!(matches!(
            self.authority.admit_turn(admit()),
            GovernedTurnOutcome::Admitted { .. }
        ));
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
            self.authority.register_action(action("action-1")),
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
        cap_id: Some("capability-1".into()),
    }
}
fn request_interaction() -> RequestInteractionRequest {
    RequestInteractionRequest {
        query_operation: false,
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
        cap_id: Some("capability-1".into()),
    }
}
fn action(action_id: &str) -> ActionRegistrationRequest {
    ActionRegistrationRequest {
        stable_operation_id: None,
        action_id: action_id.into(),
        agent_id: "agent-1".into(),
        action_family: "c04:generic".into(),
        cap_id: "capability-1".into(),
        target_scope: String::new(),
        numeric_parameters: BTreeMap::new(),
        exact_parameters: BTreeMap::new(),
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
    assert!(matches!(
        c.authority.admit_turn(admit()),
        GovernedTurnOutcome::Admitted { .. }
    ));
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
    assert!(matches!(
        c.authority.admit_turn(admit()),
        GovernedTurnOutcome::Admitted { .. }
    ));
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
    assert!(matches!(
        c.authority.admit_turn(admit()),
        GovernedTurnOutcome::Admitted { .. }
    ));
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
fn test_comp_recovery_stales_pre_crash_lease_and_allows_fresh_lease_for_turn_commit() {
    let mut c = Fixture::new();
    c.ready_to_commit();
    let Fixture {
        _root,
        authority: old_authority,
    } = c;
    let root = _root.path().to_path_buf();
    drop(old_authority);
    let mut authority =
        D1GovernedTurnAuthority::open(&root).expect("recover authority through process open");
    assert_eq!(
        authority.commit_turn(commit()),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    let mut fresh_lease = consume();
    fresh_lease.lease_epoch = 2;
    assert_eq!(
        authority.consume_interaction(fresh_lease),
        GovernedTurnOutcome::InteractionConsumed
    );
    let mut fresh_commit = commit();
    fresh_commit.lease_epoch = 2;
    assert_eq!(
        authority.commit_turn(fresh_commit),
        GovernedTurnOutcome::TurnCommitted
    );
}
#[test]
fn test_comp_turn_commit_requires_durably_bound_interaction() {
    let mut c = Fixture::new();
    let mut no_cap_admit = admit();
    no_cap_admit.cap_id = None;
    assert!(matches!(
        c.authority.admit_turn(no_cap_admit),
        GovernedTurnOutcome::Admitted { .. }
    ));
    let mut initial_lease_commit = commit();
    initial_lease_commit.lease_epoch = 0;
    initial_lease_commit.cap_id = None;
    assert_eq!(
        c.authority.commit_turn(initial_lease_commit),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
}
#[test]
fn test_comp_turn_commit_rejects_sha256_digest_mismatches() {
    let mut c = Fixture::new();
    c.ready_to_commit();
    let mut wrong_successor = commit();
    wrong_successor.successor_digest = digest(b"different successor bytes");
    assert_eq!(
        c.authority.commit_turn(wrong_successor),
        GovernedTurnOutcome::IntegrityOrProtocolFault
    );
    let mut wrong_manifest = commit();
    wrong_manifest.action_manifest_digest = digest(b"different manifest bytes");
    assert_eq!(
        c.authority.commit_turn(wrong_manifest),
        GovernedTurnOutcome::IntegrityOrProtocolFault
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
        c.authority.register_action(action("action-1")),
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
        c.authority.register_action(action("action-2")),
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
fn test_comp_scenario_12_crash_after_submission_resets_count_and_blocks_retry() {
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
    assert_eq!(c.authority.provider_submission_count(), 1);
    assert_eq!(
        c.authority.reconstruct_after_crash(),
        GovernedTurnOutcome::Reconstructed
    );
    assert_eq!(c.authority.provider_submission_count(), 0);
    assert_eq!(
        c.authority.deliver_armed_attempt(request),
        GovernedTurnOutcome::DuplicateDelivery
    );
    assert_eq!(c.authority.provider_submission_count(), 0);
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
        c.authority.register_action(action("action-2")),
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
        c.authority.register_action(action("action-2")),
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
    assert!(matches!(
        c.authority.admit_turn(AdmitTurnRequest {
            turn_id: 2,
            base_projection_digest: digest(b"durably persisted governed-turn fixture"),
            ..admit()
        }),
        GovernedTurnOutcome::Admitted { .. }
    ));
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
        c.authority.register_action(action("action-2")),
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
fn test_comp_crash_after_quarantine_keeps_different_action_locked_on_same_scope() {
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
        c.authority.register_action(action("action-2")),
        GovernedTurnOutcome::ActionRegistered
    );
    assert_eq!(
        c.authority.reconstruct_after_crash(),
        GovernedTurnOutcome::Reconstructed
    );
    let mut different_action = admission();
    different_action.action_id = "action-2".into();
    assert_eq!(
        c.authority.present_admission_certificate(different_action),
        GovernedTurnOutcome::RejectedCurrentState
    );
}

#[test]
fn test_comp_operator_resolution_persists_and_releases_quarantined_scope() {
    let mut c = Fixture::new();
    c.dispatched_action();
    assert!(matches!(
        c.authority.present_settlement_certificate(settlement()),
        GovernedTurnOutcome::Settled { .. }
    ));
    let mut conflicting = settlement();
    conflicting.evidence_region_id = "region://conflicting-evidence".into();
    conflicting.evidence_digest = digest(b"conflicting settlement evidence");
    assert_eq!(
        c.authority.present_settlement_certificate(conflicting),
        GovernedTurnOutcome::QuarantinedDispute
    );

    assert_eq!(
        c.authority
            .resolve_quarantined_dispute(ResolveQuarantinedDisputeRequest {
                attempt_id: 1,
                resolution: DisputeResolution::NotApplied,
                evidence_region_digest: Some(digest(b"operator review evidence")),
                operator_id: "operator-1".into(),
            }),
        Ok(GovernedTurnOutcome::EntryPersisted)
    );
    assert_eq!(
        c.authority.register_action(action("action-2")),
        GovernedTurnOutcome::ActionRegistered
    );
    let mut successor = admission();
    successor.action_id = "action-2".into();
    assert_eq!(
        c.authority.present_admission_certificate(successor),
        GovernedTurnOutcome::AttemptArmed { attempt_id: 2 }
    );
}

#[test]
fn test_c2_conflicting_delivery_is_ambiguous_without_submission() {
    let mut c = Fixture::new();
    c.dispatched_action();
    let before = c.authority.inspect_journal();
    assert_eq!(
        c.authority
            .deliver_armed_attempt(DeliverArmedAttemptRequest {
                attempt_id: 1,
                dispatch_identity: "conflicting-operation".into(),
            }),
        GovernedTurnOutcome::Ambiguous
    );
    assert_eq!(c.authority.inspect_journal(), before);
    assert_eq!(c.authority.provider_submission_count(), 0);
}

#[test]
fn test_c2_effect_transition_during_turn_preserves_fresh_commit_and_fence() {
    for fence in [false, true] {
        let mut c = Fixture::new();
        c.armed_action();
        let base = c
            .authority
            .storage()
            .journal_requests()
            .last()
            .map(|r| {
                c.authority
                    .storage()
                    .read_entry(&r.agent_id, r.entry_id)
                    .unwrap()
                    .entry_digest
            })
            .unwrap();
        let mut next = admit();
        next.turn_id = 2;
        next.base_projection_digest = base.clone();
        assert!(matches!(
            c.authority.admit_turn(next),
            GovernedTurnOutcome::Admitted { .. }
        ));
        assert_eq!(
            c.authority.record_dispatch_attempt(dispatch()),
            GovernedTurnOutcome::DispatchRecorded
        );
        let mut interaction = request_interaction();
        interaction.interaction_id = "c2-next-turn".into();
        let mut report = report_interaction();
        report.interaction_id = interaction.interaction_id.clone();
        let mut consumption = consume();
        consumption.interaction_id = interaction.interaction_id.clone();
        assert_eq!(
            c.authority.request_interaction(interaction),
            GovernedTurnOutcome::InteractionRequested
        );
        if fence {
            assert_eq!(
                c.authority.persist_fence(2),
                GovernedTurnOutcome::GenerationFenced { generation: 2 }
            );
        } else {
            assert_eq!(
                c.authority.report_outcome(report),
                GovernedTurnOutcome::InteractionBound
            );
            assert_eq!(
                c.authority.consume_interaction(consumption),
                GovernedTurnOutcome::InteractionConsumed
            );
            let mut request = commit();
            request.base_projection_digest = base;
            assert_eq!(
                c.authority.commit_turn(request),
                GovernedTurnOutcome::TurnCommitted
            );
        }
    }
}

#[test]
fn test_c2_snapshot_is_core_persisted_bound_and_immutable() {
    let mut c = Fixture::new();
    c.dispatched_action();
    let last = c.authority.storage().journal_requests().pop().unwrap();
    let base = c
        .authority
        .storage()
        .read_entry(&last.agent_id, last.entry_id)
        .unwrap()
        .entry_digest;
    let mut next = admit();
    next.turn_id = 2;
    next.base_projection_digest = base;
    let GovernedTurnOutcome::Admitted {
        unsettled_effects_snapshot: snapshot,
    } = c.authority.admit_turn(next)
    else {
        panic!("admission failed")
    };
    assert_eq!(snapshot.author, "Core");
    assert_eq!(snapshot.turn_id, 2);
    assert_eq!(snapshot.region_ref, "observation:unsettled_effects:2");
    assert_eq!(snapshot.attempts.len(), 1);
    assert_eq!(
        snapshot.attempts[0].stable_op_id.as_deref(),
        Some("dispatch-1")
    );
    let bytes = c
        .authority
        .storage()
        .read_region(&snapshot.region_ref)
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&bytes).unwrap(),
        serde_json::to_value(&snapshot).unwrap()
    );
    let admission = c.authority.storage().journal_requests().pop().unwrap();
    assert_eq!(admission.region_refs, vec![snapshot.region_ref.clone()]);
    assert_eq!(
        c.authority.ensure_region(
            "observation:unsettled_effects:3",
            &digest(b"forged"),
            b"forged",
            DurabilityProfile::D1
        ),
        castor_kernel::c01_storage::EnsureRegionOutcome::RejectedIdentityConflict
    );
    assert_eq!(
        c.authority.present_settlement_certificate(settlement()),
        GovernedTurnOutcome::Settled {
            resolution: "Confirmed".into()
        }
    );
    assert_eq!(
        c.authority
            .storage()
            .read_region(&snapshot.region_ref)
            .unwrap(),
        bytes
    );
    assert_eq!(
        c.authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    let last = c.authority.storage().journal_requests().pop().unwrap();
    let mut next = admit();
    next.turn_id = 3;
    next.cap_id = None;
    next.base_projection_digest = last.expected_base_projection_digest.unwrap();
    let GovernedTurnOutcome::Admitted {
        unsettled_effects_snapshot: next,
    } = c.authority.admit_turn(next)
    else {
        panic!("next admission failed")
    };
    assert!(next.attempts.is_empty());
}

#[test]
fn test_c2_failed_admission_cannot_replace_authoritative_projection() {
    let mut c = Fixture::new();
    c.armed_action();
    let mut bad = admit();
    bad.turn_id = 2;
    bad.base_projection_digest = "forged-base".into();
    let before = c.authority.inspect_journal();
    assert_eq!(
        c.authority.admit_turn(bad),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
    assert_eq!(c.authority.inspect_journal(), before);
    assert_eq!(
        c.authority.record_dispatch_attempt(dispatch()),
        GovernedTurnOutcome::DispatchRecorded
    );
}

#[test]
fn test_c2_orphan_snapshot_after_crash_does_not_poison_admission() {
    let mut c = Fixture::new();
    c.dispatched_action();
    let root = c._root.path().to_path_buf();
    drop(c.authority);
    // Crash seam: Region is durable, but no LeaseGranted references it.
    let mut storage = D1DurableStorage::open(&root).unwrap();
    let orphan = b"uncommitted pre-crash observation";
    assert!(matches!(
        storage.ensure_region(
            "observation:unsettled_effects:2",
            &digest(orphan),
            orphan,
            DurabilityProfile::D1
        ),
        castor_kernel::c01_storage::EnsureRegionOutcome::Success(_)
    ));
    drop(storage);
    let mut authority = D1GovernedTurnAuthority::open(&root).unwrap();
    let last = authority.storage().journal_requests().pop().unwrap();
    let mut next = admit();
    next.turn_id = 2;
    next.base_projection_digest = authority
        .storage()
        .read_entry(&last.agent_id, last.entry_id)
        .unwrap()
        .entry_digest;
    let outcome = authority.admit_turn(next);
    let GovernedTurnOutcome::Admitted {
        unsettled_effects_snapshot: snapshot,
    } = outcome
    else {
        panic!("{outcome:?}")
    };
    assert_eq!(snapshot.attempts[0].status, "Dispatched");
    assert!(snapshot.attempts[0].ambiguous_delivery);
    assert_ne!(
        authority
            .storage()
            .read_region(&snapshot.region_ref)
            .unwrap(),
        orphan
    );
    drop(authority);
    assert!(D1GovernedTurnAuthority::open(&root).is_ok());
}

#[test]
fn test_c2_successor_cannot_reuse_closed_turn_interaction_identity() {
    let mut c = Fixture::new();
    c.armed_action();
    let last = c.authority.storage().journal_requests().pop().unwrap();
    let mut next = admit();
    next.turn_id = 2;
    next.base_projection_digest = c
        .authority
        .storage()
        .read_entry(&last.agent_id, last.entry_id)
        .unwrap()
        .entry_digest;
    assert!(matches!(
        c.authority.admit_turn(next),
        GovernedTurnOutcome::Admitted { .. }
    ));
    let before = c.authority.inspect_journal();
    assert_eq!(
        c.authority.request_interaction(request_interaction()),
        GovernedTurnOutcome::RejectedPrecondition
    );
    assert_eq!(c.authority.inspect_journal(), before);
    let mut fresh = request_interaction();
    fresh.interaction_id = "fresh-probe".into();
    assert_eq!(
        c.authority.request_interaction(fresh),
        GovernedTurnOutcome::InteractionRequested
    );
    assert_eq!(
        c.authority.report_outcome(report_interaction()),
        GovernedTurnOutcome::RejectedLateOrClosedTurn
    );
}

#[test]
fn test_c2_registered_operation_survives_snapshot_and_rejects_rebinding() {
    for snapshot in [false, true] {
        let mut f = Fixture::new();
        f.ready_to_commit();
        assert_eq!(
            f.authority.commit_turn(commit()),
            GovernedTurnOutcome::TurnCommitted
        );
        let mut registration = action("action-1");
        registration.stable_operation_id = Some("registered-op".into());
        assert_eq!(
            f.authority.register_action(registration.clone()),
            GovernedTurnOutcome::ActionRegistered
        );
        assert_eq!(
            f.authority.present_admission_certificate(admission()),
            GovernedTurnOutcome::AttemptArmed { attempt_id: 1 }
        );
        let last = f.authority.storage().journal_requests().pop().unwrap();
        let base = f
            .authority
            .storage()
            .read_entry(&last.agent_id, last.entry_id)
            .unwrap()
            .entry_digest;
        if snapshot {
            f.authority.create_snapshot("operation-cache").unwrap();
        }
        assert_eq!(
            f.authority.reconstruct_after_crash(),
            GovernedTurnOutcome::Reconstructed
        );
        let before = f.authority.inspect_journal();
        registration.stable_operation_id = Some("replacement".into());
        assert_eq!(
            f.authority.register_action(registration),
            GovernedTurnOutcome::RejectedPrecondition
        );
        assert_eq!(
            f.authority.record_dispatch_attempt(dispatch()),
            GovernedTurnOutcome::RejectedBindingOrIssuer
        );
        assert_eq!(f.authority.inspect_journal(), before);
        let mut next = admit();
        next.turn_id = 2;
        next.base_projection_digest = base;
        let GovernedTurnOutcome::Admitted {
            unsettled_effects_snapshot,
        } = f.authority.admit_turn(next)
        else {
            panic!("admission failed")
        };
        assert_eq!(
            unsettled_effects_snapshot.attempts[0]
                .stable_op_id
                .as_deref(),
            Some("registered-op")
        );
        let mut bound = dispatch();
        bound.dispatch_identity = "registered-op".into();
        assert_eq!(
            f.authority.record_dispatch_attempt(bound),
            GovernedTurnOutcome::DispatchRecorded
        );
    }
}

#[test]
fn test_c2_probe_budget_survives_snapshot_tail_and_exhaustion() {
    let mut f = Fixture::new();
    f.armed_action();
    let last = f.authority.storage().journal_requests().pop().unwrap();
    let mut next = admit();
    next.turn_id = 2;
    next.base_projection_digest = f
        .authority
        .storage()
        .read_entry(&last.agent_id, last.entry_id)
        .unwrap()
        .entry_digest;
    assert!(matches!(
        f.authority.admit_turn(next),
        GovernedTurnOutcome::Admitted { .. }
    ));
    for index in 0..3 {
        let mut request = request_interaction();
        request.query_operation = true;
        request.interaction_id = format!("probe-{index}");
        request.lease_epoch = index;
        assert_eq!(
            f.authority.request_interaction(request.clone()),
            GovernedTurnOutcome::InteractionRequested
        );
        assert_eq!(
            f.authority.projection_summary()["recovery"]["probe_budget_remaining"],
            2 - index
        );
        let before = f.authority.inspect_journal();
        assert_eq!(
            f.authority.request_interaction(request),
            GovernedTurnOutcome::RejectedStaleAuthority
        );
        assert_eq!(f.authority.inspect_journal(), before);
        let mut report = report_interaction();
        report.interaction_id = format!("probe-{index}");
        assert_eq!(
            f.authority.report_outcome(report),
            GovernedTurnOutcome::InteractionBound
        );
        let mut consume = consume();
        consume.interaction_id = format!("probe-{index}");
        consume.lease_epoch = index + 1;
        assert_eq!(
            f.authority.consume_interaction(consume),
            GovernedTurnOutcome::InteractionConsumed
        );
    }
    f.authority.create_snapshot("budget-cache").unwrap();
    assert_eq!(
        f.authority.reconstruct_after_crash(),
        GovernedTurnOutcome::Reconstructed
    );
    assert_eq!(
        f.authority.projection_summary()["recovery"]["probe_budget_remaining"],
        0
    );
    let mut fresh_lease = consume();
    fresh_lease.interaction_id = "probe-2".into();
    fresh_lease.lease_epoch = 4;
    assert_eq!(
        f.authority.consume_interaction(fresh_lease),
        GovernedTurnOutcome::InteractionConsumed
    );
    let before = f.authority.inspect_journal();
    let mut request = request_interaction();
    request.query_operation = true;
    request.lease_epoch = 4;
    assert_eq!(
        f.authority.request_interaction(request),
        GovernedTurnOutcome::RejectedCurrentState
    );
    assert_eq!(f.authority.inspect_journal(), before);
}
