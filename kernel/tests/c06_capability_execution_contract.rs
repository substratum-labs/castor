//! RED contract harness for C-06 capability execution (EPIC-28 / T-304-B).
//!
//! These are intentionally ahead of the implementation.  They describe the
//! privileged-grant, attenuated-derive, and exercise seams that T-304-C makes
//! green; the Phase 2 API stubs fail closed.

use castor_kernel::c01_storage::{CoreEntry, D1DurableStorage, DurabilityProfile, DurableStorage};
use castor_kernel::c06_composition::{
    ActionRegistrationRequest, AdmitTurnRequest, CapabilityDeriveOutcome, CapabilityGrant,
    CapabilityRight, CommitTurnRequest, Constraint, D1GovernedTurnAuthority,
    DeriveCapabilityRequest, GovernedTurnOutcome, GrantCapabilityRequest,
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
            ("region://fixture", b"durable successor".as_slice()),
            ("region://actions", b"action-1".as_slice()),
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

    fn grant(&mut self, grant: CapabilityGrant) {
        assert_eq!(
            self.authority
                .grant_capability(GrantCapabilityRequest { grant }),
            GovernedTurnOutcome::CapabilityGranted
        );
    }

    fn admit(&mut self, agent_id: &str, cap_id: Option<&str>) -> GovernedTurnOutcome {
        self.authority.admit_turn(AdmitTurnRequest {
            agent_id: agent_id.into(),
            turn_id: 1,
            lease_epoch: 0,
            base_projection_digest: digest(b"H0"),
            cap_id: cap_id.map(str::to_owned),
        })
    }

    fn ready_to_commit(&mut self, agent_id: &str, cap_id: &str) {
        assert_eq!(
            self.admit(agent_id, Some(cap_id)),
            GovernedTurnOutcome::Admitted
        );
        assert_eq!(
            self.authority.commit_turn(CommitTurnRequest {
                lease_epoch: 0,
                base_projection_digest: digest(b"H0"),
                successor_region_id: "region://fixture".into(),
                successor_digest: digest(b"durable successor"),
                action_manifest_region_id: "region://actions".into(),
                action_manifest_digest: digest(b"action-1"),
                action_manifest: vec!["action-1".into()],
                cap_id: Some(cap_id.into()),
            }),
            GovernedTurnOutcome::TurnCommitted
        );
    }

    fn register(
        &mut self,
        cap_id: &str,
        family: &str,
        scope: &str,
        bytes: u64,
    ) -> GovernedTurnOutcome {
        self.authority.register_action(ActionRegistrationRequest {
            action_id: "action-1".into(),
            agent_id: "agent-a".into(),
            action_family: family.into(),
            cap_id: cap_id.into(),
            target_scope: scope.into(),
            numeric_parameters: BTreeMap::from([("bytes".into(), bytes)]),
        })
    }
}

fn grant(cap_id: &str, subject: &str, rights: Vec<CapabilityRight>) -> CapabilityGrant {
    CapabilityGrant {
        cap_id: cap_id.into(),
        subject: subject.into(),
        object_ref: "c04:http_get".into(),
        rights,
        constraints: vec![],
        parent_cap_id: None,
        revocation_domain: None,
        delegation_allowed: true,
        max_turns: None,
    }
}

fn derive(parent_cap_id: &str, child_rights: Vec<CapabilityRight>) -> DeriveCapabilityRequest {
    DeriveCapabilityRequest {
        parent_cap_id: parent_cap_id.into(),
        child_subject: "agent-child".into(),
        child_rights,
        child_object_ref: "c04:http_get".into(),
        child_constraints: vec![],
        child_delegation_allowed: false,
    }
}

#[test]
fn test_cap_unregistered_id_admit_rejected() {
    let mut c = Fixture::new();
    c.grant(grant("cap-1", "agent-a", vec![CapabilityRight::AdmitTurn]));
    assert_eq!(
        c.admit("agent-a", Some("cap-invented")),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn test_cap_subject_mismatch_rejected() {
    let mut c = Fixture::new();
    c.grant(grant("cap-1", "agent-a", vec![CapabilityRight::AdmitTurn]));
    assert_eq!(
        c.admit("agent-b", Some("cap-1")),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn test_cap_missing_required_right_rejected() {
    let mut c = Fixture::new();
    c.grant(grant("cap-1", "agent-a", vec![CapabilityRight::AdmitTurn]));
    c.ready_to_commit("agent-a", "cap-1");
    assert_eq!(
        c.register("cap-1", "c04:http_get", "workspace/src", 1),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn test_cap_object_mismatch_rejected() {
    let mut c = Fixture::new();
    c.grant(grant(
        "cap-1",
        "agent-a",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
    ));
    c.ready_to_commit("agent-a", "cap-1");
    assert_eq!(
        c.register("cap-1", "c04:payment_api", "workspace/src", 1),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn test_cap_scope_prefix_traversal_rejected() {
    let mut c = Fixture::new();
    let mut cap = grant(
        "cap-1",
        "agent-a",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
    );
    cap.constraints = vec![Constraint::ScopePrefix {
        prefix: "workspace/src".into(),
    }];
    c.grant(cap);
    c.ready_to_commit("agent-a", "cap-1");
    assert_eq!(
        c.register("cap-1", "c04:http_get", "workspace/src/../etc", 1),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn test_cap_numeric_constraint_exceeded_rejected() {
    let mut c = Fixture::new();
    let mut cap = grant(
        "cap-1",
        "agent-a",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
    );
    cap.constraints = vec![Constraint::NumericUpperBound {
        metric: "bytes".into(),
        limit: 1024,
    }];
    c.grant(cap);
    c.ready_to_commit("agent-a", "cap-1");
    assert_eq!(
        c.register("cap-1", "c04:http_get", "workspace/src", 2048),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn test_cap_revocation_before_arm_blocks_attempt_armed() {
    let mut c = Fixture::new();
    c.grant(grant(
        "cap-1",
        "agent-a",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
    ));
    c.ready_to_commit("agent-a", "cap-1");
    assert_eq!(
        c.register("cap-1", "c04:http_get", "workspace/src", 1),
        GovernedTurnOutcome::ActionRegistered
    );
    assert_eq!(
        c.authority.revoke_capability("cap-1"),
        GovernedTurnOutcome::CapabilityRevoked
    );
    assert_eq!(
        c.authority
            .present_admission_certificate(certificate("cap-1")),
        GovernedTurnOutcome::RejectedCapabilityRevoked
    );
    assert!(!c
        .authority
        .storage()
        .journal_requests()
        .iter()
        .any(|request| matches!(request.entry, CoreEntry::AttemptArmed { .. })));
    assert_eq!(c.authority.provider_submission_count(), 0);
}

#[test]
fn test_cap_revocation_after_arm_does_not_unarm() {
    let mut c = Fixture::new();
    c.grant(grant(
        "cap-1",
        "agent-a",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
    ));
    c.ready_to_commit("agent-a", "cap-1");
    assert_eq!(
        c.register("cap-1", "c04:http_get", "workspace/src", 1),
        GovernedTurnOutcome::ActionRegistered
    );
    assert_eq!(
        c.authority
            .present_admission_certificate(certificate("cap-1")),
        GovernedTurnOutcome::AttemptArmed { attempt_id: 1 }
    );
    assert_eq!(
        c.authority.revoke_capability("cap-1"),
        GovernedTurnOutcome::CapabilityRevoked
    );
    assert_eq!(
        c.authority.record_dispatch_attempt(dispatch()),
        GovernedTurnOutcome::DispatchRecorded
    );
    assert_eq!(
        c.authority.deliver_armed_attempt(delivery()),
        GovernedTurnOutcome::Delivered
    );
}

#[test]
fn test_cap_derivation_rights_amplification_rejected() {
    let mut c = Fixture::new();
    c.grant(grant(
        "cap-1",
        "agent-a",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::Derive],
    ));
    assert_eq!(
        c.authority.derive_capability(derive(
            "cap-1",
            vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction]
        )),
        CapabilityDeriveOutcome::Rejected(GovernedTurnOutcome::RejectedPrecondition)
    );
}

#[test]
fn test_cap_derivation_non_delegable_rejected() {
    let mut c = Fixture::new();
    let mut cap = grant("cap-1", "agent-a", vec![CapabilityRight::Derive]);
    cap.delegation_allowed = false;
    c.grant(cap);
    assert_eq!(
        c.authority
            .derive_capability(derive("cap-1", vec![CapabilityRight::Derive])),
        CapabilityDeriveOutcome::Rejected(GovernedTurnOutcome::RejectedPrecondition)
    );
}

#[test]
fn test_cap_derivation_constraint_relaxation_rejected() {
    let mut c = Fixture::new();
    let mut cap = grant("cap-1", "agent-a", vec![CapabilityRight::Derive]);
    cap.constraints = vec![Constraint::NumericUpperBound {
        metric: "spend".into(),
        limit: 100,
    }];
    c.grant(cap);
    let mut request = derive("cap-1", vec![CapabilityRight::Derive]);
    request.child_constraints = vec![Constraint::NumericUpperBound {
        metric: "spend".into(),
        limit: 200,
    }];
    assert_eq!(
        c.authority.derive_capability(request),
        CapabilityDeriveOutcome::Rejected(GovernedTurnOutcome::RejectedPrecondition)
    );
}

#[test]
fn test_cap_revocation_before_commit_blocks_turn_commit() {
    let mut c = Fixture::new();
    c.grant(grant("cap-1", "agent-a", vec![CapabilityRight::AdmitTurn]));
    assert_eq!(
        c.admit("agent-a", Some("cap-1")),
        GovernedTurnOutcome::Admitted
    );
    assert_eq!(
        c.authority.revoke_capability("cap-1"),
        GovernedTurnOutcome::CapabilityRevoked
    );
    assert_eq!(
        c.authority.commit_turn(commit("cap-1")),
        GovernedTurnOutcome::RejectedCapabilityRevoked
    );
    assert!(!c
        .authority
        .storage()
        .journal_requests()
        .iter()
        .any(|request| matches!(request.entry, CoreEntry::TurnCommitted { .. })));
}

#[test]
fn test_cap_cascading_provenance_tree_revocation() {
    let mut c = Fixture::new();
    c.grant(grant(
        "cap-1",
        "agent-a",
        vec![
            CapabilityRight::AdmitTurn,
            CapabilityRight::RegisterAction,
            CapabilityRight::Derive,
        ],
    ));
    let child = expect_derived(c.authority.derive_capability(derive(
        "cap-1",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
    )));
    assert_eq!(
        c.authority.revoke_capability("cap-1"),
        GovernedTurnOutcome::CapabilityRevoked
    );
    c.ready_to_commit("agent-a", &child);
    assert_eq!(
        c.register(&child, "c04:http_get", "workspace/src", 1),
        GovernedTurnOutcome::RejectedCapabilityRevoked
    );
}

#[test]
fn test_cap_derive_after_ancestor_revoke_rejected() {
    let mut c = Fixture::new();
    c.grant(grant("cap-1", "agent-a", vec![CapabilityRight::Derive]));
    assert_eq!(
        c.authority.revoke_capability("cap-1"),
        GovernedTurnOutcome::CapabilityRevoked
    );
    assert_eq!(
        c.authority
            .derive_capability(derive("cap-1", vec![CapabilityRight::Derive])),
        CapabilityDeriveOutcome::Rejected(GovernedTurnOutcome::RejectedCapabilityRevoked)
    );
}

#[test]
fn test_cap_duplicate_derive_idempotence() {
    let mut c = Fixture::new();
    c.grant(grant("cap-1", "agent-a", vec![CapabilityRight::Derive]));
    let first = expect_derived(
        c.authority
            .derive_capability(derive("cap-1", vec![CapabilityRight::Derive])),
    );
    let second = expect_derived(
        c.authority
            .derive_capability(derive("cap-1", vec![CapabilityRight::Derive])),
    );
    assert_eq!(first, second);
}

#[test]
fn test_cap_stale_generation_cannot_exercise_valid_grant() {
    let mut c = Fixture::new();
    c.grant(grant(
        "cap-1",
        "agent-a",
        vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
    ));
    c.ready_to_commit("agent-a", "cap-1");
    assert_eq!(
        c.authority.persist_fence(2),
        GovernedTurnOutcome::GenerationFenced { generation: 2 }
    );
    assert_eq!(
        c.register("cap-1", "c04:http_get", "workspace/src", 1),
        GovernedTurnOutcome::RejectedStaleAuthority
    );
}

fn certificate(
    capability_id: &str,
) -> castor_kernel::c06_composition::PresentAdmissionCertificateRequest {
    castor_kernel::c06_composition::PresentAdmissionCertificateRequest {
        action_id: "action-1".into(),
        target_scope: "workspace/src".into(),
        capability_id: capability_id.into(),
        generation: 1,
    }
}
fn dispatch() -> castor_kernel::c06_composition::RecordDispatchAttemptRequest {
    castor_kernel::c06_composition::RecordDispatchAttemptRequest {
        attempt_id: 1,
        dispatch_identity: "dispatch-1".into(),
    }
}
fn delivery() -> castor_kernel::c06_composition::DeliverArmedAttemptRequest {
    castor_kernel::c06_composition::DeliverArmedAttemptRequest {
        attempt_id: 1,
        dispatch_identity: "dispatch-1".into(),
    }
}
fn commit(cap_id: &str) -> CommitTurnRequest {
    CommitTurnRequest {
        lease_epoch: 0,
        base_projection_digest: digest(b"H0"),
        successor_region_id: "region://fixture".into(),
        successor_digest: digest(b"durable successor"),
        action_manifest_region_id: "region://actions".into(),
        action_manifest_digest: digest(b"action-1"),
        action_manifest: vec!["action-1".into()],
        cap_id: Some(cap_id.into()),
    }
}
fn expect_derived(outcome: CapabilityDeriveOutcome) -> String {
    match outcome {
        CapabilityDeriveOutcome::Derived { cap_id } => cap_id,
        CapabilityDeriveOutcome::Rejected(outcome) => panic!("derive rejected: {outcome:?}"),
    }
}
