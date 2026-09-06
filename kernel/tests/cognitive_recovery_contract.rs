//! EPIC-34 Phase B: executable RFC R1–R12 contracts, deliberately RED.
//!
//! R1–R10/R12 run the standard-library Python physical harness against Cargo's
//! freshly built castord (never an old release binary). Each scenario has its
//! own process and D1 directory. Python assertions and actual wire responses
//! are included in Cargo's failure output. R11 uses real C-01 durable entries
//! to reach a reconstruction-only state that v1 admission cannot generate.
//! No kernel changes, ignored tests, should_panic, or hard-coded RED results.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use castor_kernel::c06_composition::{
    ActionRegistrationRequest, D1GovernedTurnAuthority, GovernedTurnOutcome,
    PresentAdmissionCertificateRequest, PresentSettlementCertificateRequest,
};
use sha2::{Digest, Sha256};
use std::process::Command;

fn physical(scenario: &str) {
    let script = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../tests/test_cognitive_recovery_castord.py");
    let output = Command::new("python3")
        .arg(script)
        .args(["-v", &format!("CognitiveRecovery.{scenario}")])
        .env("CASTORD_BINARY", env!("CARGO_BIN_EXE_castord"))
        .output()
        .expect("Python 3 is required for the physical contract harness");
    assert!(
        output.status.success(),
        "{scenario} failed against the physical daemon ({}):\n{}\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

macro_rules! physical_contract {
    ($name:ident) => {
        #[test]
        fn $name() {
            physical(stringify!($name));
        }
    };
}
physical_contract!(test_r1_guest_certificate_denied_without_journal_mutation);
physical_contract!(test_r2_reject_mismatched_binding_or_issuer);
physical_contract!(test_r3_not_found_preserves_ambiguity_then_late_dispatch);
physical_contract!(test_r4_duplicate_confirmed_ack_no_append_no_rearm);
physical_contract!(test_r5_atomic_cancel_tombstone_rejects_delayed_arrivals);
physical_contract!(test_r6_next_turn_probe_allowed_mutation_locked);
physical_contract!(test_r7_revoked_capability_after_snapshot_rejected);
physical_contract!(test_r8_closed_query_descriptor_validation);
physical_contract!(test_r9_sigkill_arm_dispatch_evidence_and_torn_fsync);
physical_contract!(test_r10_budget_survives_restart_escalation_requires_hitl);
physical_contract!(test_r12_closed_turn_drops_late_probe_requires_fresh_lease);

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

const SCOPE: &str = "payment:fixture:merchant-42";
const REGION: &str = "region://r11/manifest";

/// C-01 reconstruction fixture, explicitly NOT a concurrent admission path.
/// All CRC frames/proofs are emitted by production DurableStorage. No mock
/// settlement projection and no ad-hoc file/CRC rewriting.
fn two_unsettled(storage: &mut D1DurableStorage) {
    let bytes = b"a1\na2\na3";
    assert!(matches!(
        storage.ensure_region(REGION, &digest(bytes), bytes, DurabilityProfile::D1),
        EnsureRegionOutcome::Success(_)
    ));
    let mut entries = vec![CoreEntry::TurnCommitted {
        turn_id: 1,
        successor_projection_digest: Some(digest(bytes)),
        action_manifest_digest: Some(digest(bytes)),
        action_manifest: vec!["a1".into(), "a2".into(), "a3".into()],
        cap_id: None,
    }];
    for id in 1..=2 {
        entries.push(CoreEntry::AttemptArmed {
            action_id: format!("a{id}"),
            attempt_id: id,
            action_region_ref: REGION.into(),
            action_digest: digest(bytes),
            request_digest: SCOPE.into(),
        });
        entries.push(CoreEntry::DispatchAttempt {
            action_id: format!("a{id}"),
            attempt_id: id,
            adapter_id: format!("op-{id}"),
        });
    }
    let mut base = None;
    for (index, entry) in entries.into_iter().enumerate() {
        let proof = match storage.append_conditional(AppendConditionalRequest {
            agent_id: "r11-agent".into(),
            entry_id: index as u64 + 1,
            expected_core_epoch: 1,
            expected_agent_generation: Some(1),
            expected_turn_id: None,
            expected_lease_epoch: None,
            expected_base_projection_digest: base,
            entry,
            region_refs: vec![REGION.into(), REGION.into()],
        }) {
            AppendConditionalOutcome::EntryPersisted(proof) => proof,
            other => panic!("R11 fixture must be durable, not a RED setup failure: {other:?}"),
        };
        base = Some(if index == 0 {
            digest(bytes)
        } else {
            proof.entry_digest
        });
    }
}

#[test]
fn test_r11_reconstructed_partial_settlement_dispute_and_alias_lock() {
    let root = tempfile::tempdir().unwrap();
    let mut storage = D1DurableStorage::open(root.path()).unwrap();
    two_unsettled(&mut storage);
    drop(storage);
    // Re-open disk and replay Core projection. for_test omits the lease epoch
    // barrier only; R9 independently covers physical startup/fencing.
    let mut core = D1GovernedTurnAuthority::for_test(D1DurableStorage::open(root.path()).unwrap());
    assert_eq!(core.projection_summary()["locked_scopes"], 1);
    assert_eq!(
        core.inspect_journal()
            .iter()
            .filter(|e| matches!(e, CoreEntry::AttemptArmed { .. }))
            .count(),
        2,
        "R11 must not be vacuous: two real unresolved journal attempts"
    );
    assert_eq!(
        core.register_action(ActionRegistrationRequest {
            action_id: "a3".into(),
            agent_id: "r11-agent".into(),
            action_family: "c04:generic".into(),
            cap_id: String::new(),
            target_scope: SCOPE.into(),
            numeric_parameters: Default::default(),
            exact_parameters: Default::default(),
        }),
        GovernedTurnOutcome::ActionRegistered
    );
    // This Core-level seam supplies already-verified certificate inputs. It
    // does not test the issuer boundary; physical R1/R2/R4/R5 do that.
    let certificate = PresentSettlementCertificateRequest {
        attempt_id: 1,
        dispatch_identity: "op-1".into(),
        evidence_region_id: REGION.into(),
        evidence_digest: digest(b"a1\na2\na3"),
        resolution: "Confirmed".into(),
        proof_class: "ProviderConfirmation".into(),
    };
    assert_eq!(
        core.present_settlement_certificate(certificate.clone()),
        GovernedTurnOutcome::Settled {
            resolution: "Confirmed".into()
        }
    );
    assert_eq!(core.projection_summary()["locked_scopes"], 1);
    let admission = PresentAdmissionCertificateRequest {
        action_id: "a3".into(),
        target_scope: SCOPE.into(),
        capability_id: "fixture-cap".into(),
        generation: 1,
    };
    assert_eq!(
        core.present_admission_certificate(admission.clone()),
        GovernedTurnOutcome::RejectedCurrentState,
        "settling A1 must not release A2's lock"
    );
    let mut conflicting = certificate;
    conflicting.resolution = "NotApplied".into();
    conflicting.proof_class = "VerifiableNonExecution".into();
    assert_eq!(
        core.present_settlement_certificate(conflicting),
        GovernedTurnOutcome::QuarantinedDispute
    );
    assert_eq!(core.projection_summary()["locked_scopes"], 1);
    assert_eq!(core.projection_summary()["quarantined_disputes"], 1);
    let before = core.inspect_journal();
    let mut alias = admission;
    // Reusing the same immutable action with a guest-selected spelling must
    // not substitute for an adapter-canonical scope. This is NOT prefix lock.
    alias.target_scope = "payment:fixture:merchant-42/alias".into();
    assert_eq!(
        core.present_admission_certificate(alias),
        GovernedTurnOutcome::RejectedCurrentState
    );
    assert_eq!(core.inspect_journal(), before);
}
