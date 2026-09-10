//! T-320-D2 contract: only the bound actuator may acquire one Attempt's payload.

use castor_kernel::c01_storage::{
    ActionBinding, CoreEntry, D1DurableStorage, DurabilityProfile, DurableStorage,
    EnsureRegionOutcome,
};
use castor_kernel::c06_composition::{
    AcquireDispatchRequest, ActionRegistrationRequest, AdmitTurnRequest, CapabilityGrant,
    CapabilityRight, CommitTurnRequest, ConsumeInteractionRequest, D1GovernedTurnAuthority,
    DeliveredActionEnvelope, GovernedTurnOutcome, GrantCapabilityRequest, InteractionOutcomeReport,
    PresentAdmissionCertificateRequest, PresentSettlementCertificateRequest,
    RecordDispatchAttemptRequest, RequestInteractionRequest,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use tempfile::TempDir;

const AGENT: &str = "agent-actuator-test";
const ACTION: &str = "write-client";
const ACTUATOR: &str = "repo-workspace-actuator";
const DISPATCH: &str = "op-write-client";
const PAYLOAD_REGION: &str = "region://dogfood/client";
const PAYLOAD: &[u8] = b"typed client payload";

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

struct Fixture {
    _root: TempDir,
    authority: D1GovernedTurnAuthority,
}

impl Fixture {
    fn dispatched() -> Self {
        let root = tempfile::tempdir().expect("temporary actuator root");
        let mut storage = D1DurableStorage::open(root.path()).expect("open D1 storage");
        for (region, bytes) in [
            ("region://successor", b"successor".as_slice()),
            ("region://manifest", ACTION.as_bytes()),
            (PAYLOAD_REGION, PAYLOAD),
            ("region://observation", b"observation".as_slice()),
            ("region://settlement", b"confirmed".as_slice()),
        ] {
            assert!(matches!(
                storage.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
                EnsureRegionOutcome::Success(_)
            ));
        }
        let mut authority = D1GovernedTurnAuthority::for_test(storage);
        assert_eq!(
            authority.grant_capability(GrantCapabilityRequest {
                grant: CapabilityGrant {
                    cap_id: "cap-actuator".into(),
                    subject: AGENT.into(),
                    object_ref: ACTUATOR.into(),
                    rights: vec![CapabilityRight::AdmitTurn, CapabilityRight::RegisterAction],
                    constraints: vec![],
                    parent_cap_id: None,
                    revocation_domain: None,
                    delegation_allowed: false,
                    max_turns: None,
                },
            }),
            GovernedTurnOutcome::CapabilityGranted
        );
        assert!(matches!(
            authority.admit_turn(AdmitTurnRequest {
                agent_id: AGENT.into(),
                turn_id: 1,
                lease_epoch: 0,
                base_projection_digest: digest(b"base"),
                cap_id: Some("cap-actuator".into()),
            }),
            GovernedTurnOutcome::Admitted { .. }
        ));
        assert_eq!(
            authority.request_interaction(RequestInteractionRequest {
                query_operation: None,
                interaction_id: "interaction-1".into(),
                lease_epoch: 0,
                request_digest: digest(b"request"),
            }),
            GovernedTurnOutcome::InteractionRequested
        );
        assert_eq!(
            authority.report_outcome(InteractionOutcomeReport {
                interaction_id: "interaction-1".into(),
                observation_region_id: "region://observation".into(),
                observation_digest: digest(b"observation"),
            }),
            GovernedTurnOutcome::InteractionBound
        );
        assert_eq!(
            authority.consume_interaction(ConsumeInteractionRequest {
                interaction_id: "interaction-1".into(),
                lease_epoch: 1,
            }),
            GovernedTurnOutcome::InteractionConsumed
        );
        assert_eq!(
            authority.commit_turn(CommitTurnRequest {
                lease_epoch: 1,
                base_projection_digest: digest(b"base"),
                successor_region_id: "region://successor".into(),
                successor_digest: digest(b"successor"),
                action_manifest_region_id: "region://manifest".into(),
                action_manifest_digest: digest(ACTION.as_bytes()),
                action_manifest: vec![ACTION.into()],
                action_bindings: vec![ActionBinding {
                    action_id: ACTION.into(),
                    payload_region_ref: PAYLOAD_REGION.into(),
                    payload_digest: digest(PAYLOAD),
                    actuator_id: ACTUATOR.into(),
                }],
                cap_id: Some("cap-actuator".into()),
            }),
            GovernedTurnOutcome::TurnCommitted
        );
        assert_eq!(
            authority.register_action(ActionRegistrationRequest {
                stable_operation_id: Some(DISPATCH.into()),
                action_id: ACTION.into(),
                agent_id: AGENT.into(),
                action_family: ACTUATOR.into(),
                cap_id: "cap-actuator".into(),
                target_scope: "repo:castor:file/src/castor/ipc_client.py".into(),
                numeric_parameters: BTreeMap::new(),
                exact_parameters: BTreeMap::new(),
            }),
            GovernedTurnOutcome::ActionRegistered
        );
        assert_eq!(
            authority.present_admission_certificate(PresentAdmissionCertificateRequest {
                action_id: ACTION.into(),
                target_scope: "repo:castor:file/src/castor/ipc_client.py".into(),
                capability_id: "cap-actuator".into(),
                generation: 1,
            }),
            GovernedTurnOutcome::AttemptArmed { attempt_id: 1 }
        );
        assert_eq!(
            authority.record_dispatch_attempt(RecordDispatchAttemptRequest {
                attempt_id: 1,
                dispatch_identity: DISPATCH.into(),
            }),
            GovernedTurnOutcome::DispatchRecorded
        );
        Self {
            _root: root,
            authority,
        }
    }

    fn acquire(
        &mut self,
        attempt_id: u64,
        dispatch: &str,
        actuator: &str,
    ) -> Result<DeliveredActionEnvelope, GovernedTurnOutcome> {
        self.authority.acquire_dispatch(AcquireDispatchRequest {
            attempt_id,
            dispatch_identity: dispatch.into(),
            actuator_id: actuator.into(),
        })
    }
}

#[test]
fn matching_actuator_acquires_exact_bound_payload_after_durable_delivery() {
    let mut fixture = Fixture::dispatched();
    let envelope = fixture
        .acquire(1, DISPATCH, ACTUATOR)
        .expect("matching actuator acquires payload");
    assert_eq!(envelope.delivery_outcome, "Delivered");
    assert_eq!(envelope.attempt_id, 1);
    assert_eq!(envelope.action_id, ACTION);
    assert_eq!(envelope.dispatch_identity, DISPATCH);
    assert_eq!(
        envelope.target_scope,
        "repo:castor:file/src/castor/ipc_client.py"
    );
    assert_eq!(envelope.payload_region_ref, PAYLOAD_REGION);
    assert_eq!(envelope.payload_digest, digest(PAYLOAD));
    assert_eq!(envelope.actuator_id, ACTUATOR);
    assert_eq!(envelope.payload, PAYLOAD);
    assert!(fixture
        .authority
        .inspect_journal()
        .iter()
        .any(|entry| matches!(
            entry,
            CoreEntry::AdapterSubmissionRecorded { attempt_id: 1 }
        )));
}

#[test]
fn duplicate_unsettled_acquisition_returns_same_binding_and_bytes() {
    let mut fixture = Fixture::dispatched();
    let first = fixture
        .acquire(1, DISPATCH, ACTUATOR)
        .expect("first acquisition");
    let duplicate = fixture
        .acquire(1, DISPATCH, ACTUATOR)
        .expect("duplicate acquisition");
    assert_eq!(first.delivery_outcome, "Delivered");
    assert_eq!(duplicate.delivery_outcome, "DuplicateDelivery");
    assert_eq!(duplicate.payload, first.payload);
    assert_eq!(duplicate.payload_region_ref, first.payload_region_ref);
    assert_eq!(duplicate.payload_digest, first.payload_digest);
    assert_eq!(duplicate.actuator_id, first.actuator_id);
    assert_eq!(duplicate.dispatch_identity, first.dispatch_identity);
}

#[test]
fn mismatched_or_unknown_acquisition_returns_no_payload_and_writes_nothing() {
    for (attempt_id, dispatch, actuator, expected) in [
        (
            1,
            "wrong-dispatch",
            ACTUATOR,
            GovernedTurnOutcome::RejectedBindingOrIssuer,
        ),
        (
            1,
            DISPATCH,
            "wrong-actuator",
            GovernedTurnOutcome::RejectedBindingOrIssuer,
        ),
        (
            99,
            DISPATCH,
            ACTUATOR,
            GovernedTurnOutcome::RejectedCurrentState,
        ),
    ] {
        let mut fixture = Fixture::dispatched();
        let before = fixture.authority.inspect_journal().len();
        assert_eq!(
            fixture.acquire(attempt_id, dispatch, actuator),
            Err(expected)
        );
        assert_eq!(fixture.authority.inspect_journal().len(), before);
    }
}

#[test]
fn terminal_attempt_cannot_be_reacquired() {
    let mut fixture = Fixture::dispatched();
    fixture
        .acquire(1, DISPATCH, ACTUATOR)
        .expect("initial acquisition");
    assert_eq!(
        fixture
            .authority
            .present_settlement_certificate(PresentSettlementCertificateRequest {
                attempt_id: 1,
                dispatch_identity: DISPATCH.into(),
                evidence_region_id: "region://settlement".into(),
                evidence_digest: digest(b"confirmed"),
                resolution: "Confirmed".into(),
                proof_class: "ProviderConfirmation".into(),
            }),
        GovernedTurnOutcome::Settled {
            resolution: "Confirmed".into()
        }
    );
    let before = fixture.authority.inspect_journal().len();
    assert_eq!(
        fixture.acquire(1, DISPATCH, ACTUATOR),
        Err(GovernedTurnOutcome::RejectedCurrentState)
    );
    assert_eq!(fixture.authority.inspect_journal().len(), before);
}
