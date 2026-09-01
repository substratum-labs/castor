//! Hostile Test Harness for C-01 Durable Storage & C-04 Effect Adapter (T-288-A)
//!
//! Expresses the target production assertions for the 4 mandatory hostile traces:
//! 1. Normal delivery flow
//! 2. Duplicate delivery deduplication
//! 3. Lost acknowledgement recovery
//! 4. Crash-after-possible-submit recovery
//!
//! Note: These tests compile cleanly but MUST FAIL against the pre-implementation stubs
//! (PreImplementationDurableStorage & PreImplementationEffectAdapter), providing the red test suite
//! required before Phase 2 / Phase 3 implementation.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, CoreEntry, DurabilityProfile, DurableStorage, EnsureRegionOutcome,
    PersistedEntryProof, PreImplementationDurableStorage, RegionPersisted,
};
use castor_kernel::c04_adapter::{
    DeliverOutcome, DispatchCommand, EffectAdapter, ExternalKnowledge,
    PreImplementationEffectAdapter,
};

fn mock_proof(agent_id: &str, entry_id: u64, kind: &str) -> PersistedEntryProof {
    PersistedEntryProof {
        agent_id: agent_id.to_string(),
        entry_id,
        entry_digest: format!("digest-{kind}-{entry_id}"),
        entry_kind: kind.to_string(),
        durability_profile: DurabilityProfile::D1,
        expected_projection_digest: "projection_hash_001".to_string(),
        referenced_region_digests: vec!["region_hash_100".to_string()],
    }
}

fn mock_dispatch_command(agent_id: &str, attempt_id: u64) -> DispatchCommand {
    DispatchCommand {
        agent_id: agent_id.to_string(),
        action_id: "action_payment_01".to_string(),
        attempt_id,
        action_digest: "action_hash_001".to_string(),
        request_digest: "request_hash_001".to_string(),
        adapter_id: "http_payment_adapter".to_string(),
        assurance_profile: "at_least_once".to_string(),
        attempt_armed_proof: mock_proof(agent_id, 10, "AttemptArmed"),
        dispatch_proof: mock_proof(agent_id, 11, "DispatchAttempt"),
        authority_binding_digest: "binding_hash_999".to_string(),
    }
}

#[test]
fn test_hostile_trace_01_normal_delivery() {
    let mut storage = PreImplementationDurableStorage::new();
    let mut adapter = PreImplementationEffectAdapter::new();

    // Step 1: Target C-01 EnsureRegion must succeed with RegionPersisted
    let region_res = storage.ensure_region("region_100", "region_hash_100", DurabilityProfile::D1);
    assert_eq!(
        region_res,
        EnsureRegionOutcome::Success(RegionPersisted {
            region_ref: "region_100".to_string(),
            content_digest: "region_hash_100".to_string(),
            profile: DurabilityProfile::D1,
        })
    );

    // Step 2: Target C-01 AppendConditional must persist AttemptArmed entry and return PersistedEntryProof
    let entry_res = storage.append_conditional(
        "agent_alpha",
        1,
        CoreEntry::AttemptArmed {
            action_id: "action_payment_01".to_string(),
            attempt_id: 1,
            request_digest: "request_hash_001".to_string(),
        },
        &["region_100".to_string()],
    );
    let expected_armed_proof = mock_proof("agent_alpha", 1, "AttemptArmed");
    assert_eq!(
        entry_res,
        AppendConditionalOutcome::EntryPersisted(expected_armed_proof)
    );

    // Step 3: Target C-04 DeliverArmedAttempt must submit and return SubmissionObserved
    let command = mock_dispatch_command("agent_alpha", 1);
    let deliver_res = adapter.deliver_armed_attempt(command);
    assert_eq!(
        deliver_res,
        DeliverOutcome::SubmissionObserved {
            accepted_and_durable: true,
            external_knowledge: ExternalKnowledge::Observed,
        }
    );
}

#[test]
fn test_hostile_trace_02_duplicate_delivery() {
    let mut adapter = PreImplementationEffectAdapter::new();
    let command_1 = mock_dispatch_command("agent_beta", 42);
    let command_2 = mock_dispatch_command("agent_beta", 42);

    let res_1 = adapter.deliver_armed_attempt(command_1);
    assert_eq!(
        res_1,
        DeliverOutcome::SubmissionObserved {
            accepted_and_durable: true,
            external_knowledge: ExternalKnowledge::Observed,
        }
    );

    // Target C-04 deduplication: second delivery of same attempt_id must return DuplicateDelivery
    let res_2 = adapter.deliver_armed_attempt(command_2);
    assert_eq!(
        res_2,
        DeliverOutcome::DuplicateDelivery {
            accepted_and_durable: true,
            prior_external_knowledge: ExternalKnowledge::Observed,
        }
    );
}

#[test]
fn test_hostile_trace_03_lost_acknowledgement_recovery() {
    let mut storage = PreImplementationDurableStorage::new();

    let entry = CoreEntry::TurnCommitted { turn_id: 105 };

    // Initial append (suppose it persisted in storage, but acknowledgement was lost in transit)
    let append_res = storage.append_conditional("agent_gamma", 1, entry.clone(), &[]);
    let expected_proof = mock_proof("agent_gamma", 105, "TurnCommitted");
    assert_eq!(
        append_res,
        AppendConditionalOutcome::EntryPersisted(expected_proof.clone())
    );

    // Retry after lost ACK: storage must return AlreadyPersistedSameEntry
    let retry_res = storage.append_conditional("agent_gamma", 1, entry, &[]);
    assert_eq!(
        retry_res,
        AppendConditionalOutcome::AlreadyPersistedSameEntry(expected_proof.clone())
    );

    // Journal recovery lookup: read_entry must return the persisted entry proof
    let recovered_proof = storage
        .read_entry("agent_gamma", 105)
        .expect("lost ACK recovery must expose persisted entry proof from journal");
    assert_eq!(recovered_proof, expected_proof);
}

#[test]
fn test_hostile_trace_04_crash_after_possible_submit() {
    let mut adapter = PreImplementationEffectAdapter::new();
    let command = mock_dispatch_command("agent_delta", 99);

    // First delivery before crash: submission attempted but external knowledge is unknown due to crash
    let res_before_crash = adapter.deliver_armed_attempt(command.clone());
    assert_eq!(
        res_before_crash,
        DeliverOutcome::SubmissionObserved {
            accepted_and_durable: true,
            external_knowledge: ExternalKnowledge::Unknown,
        }
    );

    // Simulated restart of adapter instance (transient memory wiped)
    let mut restarted_adapter = PreImplementationEffectAdapter::new();
    let res_after_restart = restarted_adapter.deliver_armed_attempt(command);

    // Target C-04 recovery requirement: redelivery after crash must return Ambiguous or DuplicateDelivery, NEVER fresh submit
    assert!(
        matches!(
            res_after_restart,
            DeliverOutcome::Ambiguous {
                accepted_and_durable: true
            } | DeliverOutcome::DuplicateDelivery {
                accepted_and_durable: true,
                ..
            }
        ),
        "Redelivery after crash must yield Ambiguous or DuplicateDelivery, got {:?}",
        res_after_restart
    );
}
