//! T-320-D2 contract: only the bound actuator may acquire one Attempt's payload.

use castor_kernel::c01_storage::{
    ActionBinding, AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use castor_kernel::c06_composition::{
    AcquireDispatchRequest, ActionRegistrationRequest, AdmitTurnRequest, CapabilityGrant,
    CapabilityRight, CommitTurnRequest, ConsumeInteractionRequest, D1GovernedTurnAuthority,
    DeliveredActionEnvelope, GovernedTurnOutcome, GrantCapabilityRequest, InteractionOutcomeReport,
    PresentAdmissionCertificateRequest, PresentSettlementCertificateRequest,
    RecordDispatchAttemptRequest, RequestInteractionRequest,
};
use castor_kernel::host::{GatewayClient, SyscallRequest};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};
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

fn daemon_command(root: &TempDir) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_castord"));
    command.args([
        "--storage-root",
        root.path().to_str().unwrap(),
        "--socket",
        root.path().join("agent.sock").to_str().unwrap(),
        "--actuator-socket",
        root.path().join("actuator.sock").to_str().unwrap(),
    ]);
    command
}

fn write_actuator_trust(root: &TempDir, value: serde_json::Value) -> std::path::PathBuf {
    let path = root.path().join("actuator-trust.json");
    fs::write(&path, serde_json::to_vec(&value).expect("trust JSON")).expect("write trust config");
    path
}

#[test]
fn actuator_socket_requires_strict_nonempty_trust_configuration() {
    let missing_root = tempfile::tempdir().expect("missing trust root");
    let missing = daemon_command(&missing_root)
        .env_remove("CASTORD_ACTUATOR_TRUST_CONFIG")
        .output()
        .expect("run daemon without actuator trust");
    assert!(!missing.status.success());
    assert!(
        String::from_utf8_lossy(&missing.stderr).contains("requires CASTORD_ACTUATOR_TRUST_CONFIG")
    );

    for value in [
        serde_json::json!({ "peer_uid": 1, "actuator_id": "  " }),
        serde_json::json!({ "peer_uid": 1, "actuator_id": ACTUATOR, "extra": true }),
    ] {
        let root = tempfile::tempdir().expect("invalid trust root");
        let trust = write_actuator_trust(&root, value);
        let output = daemon_command(&root)
            .env("CASTORD_ACTUATOR_TRUST_CONFIG", trust)
            .output()
            .expect("run daemon with invalid actuator trust");
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("actuator trust"));
    }
}

#[test]
fn all_configured_socket_paths_must_be_distinct() {
    let root = tempfile::tempdir().expect("duplicate socket root");
    let trust = write_actuator_trust(
        &root,
        serde_json::json!({
            "peer_uid": fs::metadata(root.path()).expect("root metadata").uid(),
            "actuator_id": ACTUATOR
        }),
    );
    let same_socket = root.path().join("same.sock");
    let output = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            root.path().to_str().unwrap(),
            "--socket",
            same_socket.to_str().unwrap(),
            "--actuator-socket",
            same_socket.to_str().unwrap(),
        ])
        .env("CASTORD_ACTUATOR_TRUST_CONFIG", trust)
        .output()
        .expect("run daemon with duplicate socket paths");
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("distinct paths"));
}

#[test]
fn unknown_delivery_fault_point_is_rejected_when_test_hooks_are_enabled() {
    let root = tempfile::tempdir().expect("unknown fault root");
    let trust = write_actuator_trust(
        &root,
        serde_json::json!({
            "peer_uid": fs::metadata(root.path()).expect("root metadata").uid(),
            "actuator_id": ACTUATOR
        }),
    );
    let output = daemon_command(&root)
        .arg("--allow-test-opcodes")
        .env("CASTORD_ACTUATOR_TRUST_CONFIG", trust)
        .env("CASTORD_TEST_FAULT_POINT", "unknown-seam")
        .output()
        .expect("run daemon with unknown delivery fault point");
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("unsupported CASTORD_TEST_FAULT_POINT")
    );
}

#[test]
fn actuator_socket_rejects_a_peer_uid_not_named_by_trust() {
    let root = tempfile::tempdir().expect("wrong peer root");
    let current_uid = fs::metadata(root.path()).expect("root metadata").uid();
    let trust = write_actuator_trust(
        &root,
        serde_json::json!({
            "peer_uid": current_uid.saturating_add(1),
            "actuator_id": ACTUATOR
        }),
    );
    let actuator_socket = root.path().join("actuator.sock");
    let mut daemon = daemon_command(&root)
        .env("CASTORD_ACTUATOR_TRUST_CONFIG", trust)
        .spawn()
        .expect("start daemon with nonmatching peer uid");
    let deadline = Instant::now() + Duration::from_secs(3);
    while UnixStream::connect(&actuator_socket).is_err() {
        assert!(Instant::now() < deadline, "actuator socket start timeout");
        thread::sleep(Duration::from_millis(10));
    }
    let mut client = GatewayClient::connect(&actuator_socket).expect("connect actuator socket");
    let response = client
        .request(&SyscallRequest {
            request_id: "wrong-peer".into(),
            op: "AcquireDispatch".into(),
            payload: serde_json::json!({
                "attempt_id": 1,
                "dispatch_identity": DISPATCH,
                "actuator_id": ACTUATOR
            }),
        })
        .expect("wrong peer receives framed rejection");
    assert_eq!(response.status, "Error");
    assert_eq!(
        response.error.expect("wrong peer error").code,
        "RejectedBindingOrIssuer"
    );
    let _ = daemon.kill();
    let _ = daemon.wait();
}

#[test]
fn replayed_reservation_without_submission_is_ambiguous_and_returns_no_payload() {
    let Fixture {
        _root: root,
        authority,
    } = Fixture::dispatched();
    drop(authority);
    let mut storage = D1DurableStorage::open(root.path()).expect("open dispatched history");
    let last = storage
        .journal_requests()
        .pop()
        .expect("dispatch journal request");
    let dispatch_proof = storage
        .read_entry(&last.agent_id, last.entry_id)
        .expect("dispatch proof");
    assert!(matches!(last.entry, CoreEntry::DispatchAttempt { .. }));
    assert!(matches!(
        storage.append_conditional(AppendConditionalRequest {
            agent_id: AGENT.into(),
            entry_id: last.entry_id + 1,
            expected_core_epoch: 1,
            expected_agent_generation: Some(1),
            expected_turn_id: None,
            expected_lease_epoch: None,
            expected_base_projection_digest: Some(dispatch_proof.entry_digest),
            entry: CoreEntry::AdapterReservation { attempt_id: 1 },
            region_refs: vec![],
        }),
        AppendConditionalOutcome::EntryPersisted(_)
    ));
    drop(storage);

    let trust = write_actuator_trust(
        &root,
        serde_json::json!({
            "peer_uid": fs::metadata(root.path()).expect("root metadata").uid(),
            "actuator_id": ACTUATOR
        }),
    );
    let actuator_socket = root.path().join("actuator.sock");
    let mut daemon = daemon_command(&root)
        .env("CASTORD_ACTUATOR_TRUST_CONFIG", trust)
        .spawn()
        .expect("start daemon over ambiguous history");
    let deadline = Instant::now() + Duration::from_secs(3);
    while UnixStream::connect(&actuator_socket).is_err() {
        assert!(Instant::now() < deadline, "actuator socket start timeout");
        thread::sleep(Duration::from_millis(10));
    }
    let mut client = GatewayClient::connect(&actuator_socket).expect("connect actuator socket");
    let response = client
        .request(&SyscallRequest {
            request_id: "ambiguous-acquire".into(),
            op: "AcquireDispatch".into(),
            payload: serde_json::json!({
                "attempt_id": 1,
                "dispatch_identity": DISPATCH,
                "actuator_id": ACTUATOR
            }),
        })
        .expect("ambiguous attempt receives framed outcome");
    assert_eq!(response.status, "Ok");
    assert_eq!(
        response.outcome,
        Some(serde_json::json!({ "type": "Ambiguous" }))
    );
    let _ = daemon.kill();
    let _ = daemon.wait();
}
