//! C1 durable scope and legacy settlement migration regressions.
use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage,
};
use castor_kernel::c06_composition::{
    ActionRegistrationRequest, D1GovernedTurnAuthority, GovernedTurnOutcome,
    PresentAdmissionCertificateRequest, PresentSettlementCertificateRequest,
};
use sha2::{Digest, Sha256};

const SCOPE: &str = "payment:merchant";

fn fixture(root: &std::path::Path, legacy_settlement: bool) -> D1GovernedTurnAuthority {
    fixture_with_scope(root, legacy_settlement, Some(SCOPE.into()))
}

fn fixture_with_scope(
    root: &std::path::Path,
    legacy_settlement: bool,
    scope: Option<String>,
) -> D1GovernedTurnAuthority {
    let mut storage = D1DurableStorage::open(root).unwrap();
    let digest = format!("sha256:{:x}", Sha256::digest(b"a1\na2"));
    storage.ensure_region("manifest", &digest, b"a1\na2", DurabilityProfile::D1);
    let mut entries = vec![
        CoreEntry::TurnCommitted {
            turn_id: 1,
            successor_projection_digest: Some(digest.clone()),
            action_manifest_digest: Some(digest.clone()),
            action_manifest: vec!["a1".into(), "a2".into()],
            cap_id: None,
        },
        CoreEntry::ActionRegistered {
            stable_operation_id: None,
            action_id: "a2".into(),
            cap_id: String::new(),
            target_scope: scope,
        },
        CoreEntry::AttemptArmed {
            action_id: "a1".into(),
            attempt_id: 1,
            action_region_ref: "manifest".into(),
            action_digest: digest.clone(),
            request_digest: SCOPE.into(),
        },
        CoreEntry::DispatchAttempt {
            action_id: "a1".into(),
            attempt_id: 1,
            adapter_id: "op-1".into(),
        },
    ];
    if legacy_settlement {
        // Deserializing the historical wire shape must default to unauthenticated.
        entries.push(
            serde_json::from_value(serde_json::json!({"AttemptSettled": {
                "action_id": "a1", "attempt_id": 1, "resolution": "Confirmed",
                "evidence_region_id": "manifest", "evidence_digest": digest,
            }}))
            .unwrap(),
        );
    }
    let mut base = None;
    for (index, entry) in entries.into_iter().enumerate() {
        let result = storage.append_conditional(AppendConditionalRequest {
            agent_id: "agent".into(),
            entry_id: index as u64 + 1,
            expected_core_epoch: 1,
            expected_agent_generation: Some(1),
            expected_turn_id: None,
            expected_lease_epoch: None,
            expected_base_projection_digest: base,
            entry,
            region_refs: vec!["manifest".into(), "manifest".into()],
        });
        let AppendConditionalOutcome::EntryPersisted(proof) = result else {
            panic!("{result:?}")
        };
        base = Some(if index == 0 {
            digest.clone()
        } else {
            proof.entry_digest
        });
    }
    D1GovernedTurnAuthority::for_test(storage)
}

fn alias_admission(core: &mut D1GovernedTurnAuthority) {
    let before = core.inspect_journal();
    assert_eq!(
        core.present_admission_certificate(PresentAdmissionCertificateRequest {
            action_id: "a2".into(),
            target_scope: "guest-alias".into(),
            capability_id: String::new(),
            generation: 1,
        }),
        GovernedTurnOutcome::RejectedCurrentState
    );
    assert_eq!(core.inspect_journal(), before);
}

#[test]
fn canonical_scope_survives_genesis_and_snapshot_replay() {
    for snapshot in [false, true] {
        let root = tempfile::tempdir().unwrap();
        let mut core = fixture(root.path(), false);
        if snapshot {
            core.create_snapshot("scope-cache").unwrap();
        }
        drop(core);
        let mut core =
            D1GovernedTurnAuthority::for_test(D1DurableStorage::open(root.path()).unwrap());
        alias_admission(&mut core);
        let before = core.inspect_journal();
        assert_eq!(
            core.register_action(ActionRegistrationRequest {
                stable_operation_id: None,
                action_id: "a2".into(),
                agent_id: "agent".into(),
                action_family: "adapter".into(),
                cap_id: String::new(),
                target_scope: "guest-alias".into(),
                numeric_parameters: Default::default(),
                exact_parameters: Default::default(),
            }),
            GovernedTurnOutcome::RejectedPrecondition
        );
        assert_eq!(core.inspect_journal(), before);
    }
}

#[test]
fn legacy_guest_settlement_keeps_scope_locked_after_replay_and_snapshot() {
    let root = tempfile::tempdir().unwrap();
    let mut core = fixture(root.path(), true);
    assert_eq!(core.projection_summary()["locked_scopes"], 1);
    assert_eq!(core.projection_summary()["quarantined_disputes"], 1);
    alias_admission(&mut core);
    core.create_snapshot("legacy-cache").unwrap();
    drop(core);
    let mut core = D1GovernedTurnAuthority::for_test(D1DurableStorage::open(root.path()).unwrap());
    alias_admission(&mut core);
    assert_eq!(core.projection_summary()["quarantined_disputes"], 1);
}

#[test]
fn identical_receipt_under_another_region_is_idempotent() {
    let root = tempfile::tempdir().unwrap();
    let mut core = fixture(root.path(), false);
    let digest = format!("sha256:{:x}", Sha256::digest(b"a1\na2"));
    core.ensure_region("receipt-copy", &digest, b"a1\na2", DurabilityProfile::D1);
    let mut cert = PresentSettlementCertificateRequest {
        attempt_id: 1,
        dispatch_identity: "op-1".into(),
        evidence_region_id: "manifest".into(),
        evidence_digest: digest,
        resolution: "Confirmed".into(),
        proof_class: "ProviderConfirmation".into(),
    };
    let expected = GovernedTurnOutcome::Settled {
        resolution: "Confirmed".into(),
    };
    assert_eq!(core.present_settlement_certificate(cert.clone()), expected);
    let before = core.inspect_journal();
    cert.evidence_region_id = "receipt-copy".into();
    assert_eq!(core.present_settlement_certificate(cert.clone()), expected);
    assert_eq!(core.inspect_journal(), before);
    drop(core);
    let mut core = D1GovernedTurnAuthority::for_test(D1DurableStorage::open(root.path()).unwrap());
    assert_eq!(core.present_settlement_certificate(cert), expected);
    assert_eq!(core.inspect_journal(), before);
}

#[test]
fn legacy_registration_without_canonical_scope_cannot_use_guest_scope() {
    let root = tempfile::tempdir().unwrap();
    let mut core = fixture_with_scope(root.path(), false, None);
    let before = core.inspect_journal();
    assert_eq!(
        core.present_admission_certificate(PresentAdmissionCertificateRequest {
            action_id: "a2".into(),
            target_scope: "guest-alias".into(),
            capability_id: String::new(),
            generation: 1,
        }),
        GovernedTurnOutcome::RejectedPrecondition
    );
    assert_eq!(core.inspect_journal(), before);
}

#[test]
fn physical_peer_credentials_and_canonical_registration() {
    let output = std::process::Command::new("python3")
        .arg(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../tests/test_evidence_boundary_castord.py"),
        )
        .arg("-v")
        .env("CASTORD_BINARY", env!("CARGO_BIN_EXE_castord"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
