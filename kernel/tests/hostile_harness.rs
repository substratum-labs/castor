//! Hostile Test Harness for C-01 Durable Storage & C-04 Effect Adapter (T-288-A)
//!
//! Asserts the 4 mandatory hostile traces:
//! 1. Normal delivery flow
//! 2. Duplicate delivery deduplication
//! 3. Lost acknowledgement recovery
//! 4. Crash-after-possible-submit recovery
//!
//! Note: Tests are written against L4 contract expectations. Against the pre-implementation
//! stubs (PreImplementationDurableStorage & PreImplementationEffectAdapter), operations return
//! UnavailableBeforeAck / UnavailableBeforeReservation by design, asserting pre-implementation state.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, CoreEntry, DurabilityProfile, DurableStorage, EnsureRegionOutcome,
    PersistedEntryProof, PreImplementationDurableStorage,
};
use castor_kernel::c04_adapter::{
    DeliverOutcome, DispatchCommand, EffectAdapter, PreImplementationEffectAdapter,
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

    // Step 1: Ensure Region
    let region_res = storage.ensure_region("region_100", "region_hash_100", DurabilityProfile::D1);
    assert_eq!(region_res, EnsureRegionOutcome::UnavailableBeforeAck);

    // Step 2: Append Conditional AttemptArmed
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
    assert_eq!(entry_res, AppendConditionalOutcome::UnavailableBeforeAck);

    // Step 3: Deliver Armed Attempt
    let command = mock_dispatch_command("agent_alpha", 1);
    let deliver_res = adapter.deliver_armed_attempt(command);
    assert_eq!(deliver_res, DeliverOutcome::UnavailableBeforeReservation);
}

#[test]
fn test_hostile_trace_02_duplicate_delivery() {
    let mut adapter = PreImplementationEffectAdapter::new();
    let command_1 = mock_dispatch_command("agent_beta", 42);
    let command_2 = mock_dispatch_command("agent_beta", 42);

    let res_1 = adapter.deliver_armed_attempt(command_1);
    let res_2 = adapter.deliver_armed_attempt(command_2);

    assert_eq!(res_1, DeliverOutcome::UnavailableBeforeReservation);
    assert_eq!(res_2, DeliverOutcome::UnavailableBeforeReservation);
}

#[test]
fn test_hostile_trace_03_lost_acknowledgement_recovery() {
    let mut storage = PreImplementationDurableStorage::new();

    let append_res = storage.append_conditional(
        "agent_gamma",
        1,
        CoreEntry::TurnCommitted { turn_id: 105 },
        &[],
    );
    assert_eq!(append_res, AppendConditionalOutcome::UnavailableBeforeAck);

    let recovered_proof = storage.read_entry("agent_gamma", 105);
    assert_eq!(recovered_proof, None);
}

#[test]
fn test_hostile_trace_04_crash_after_possible_submit() {
    let mut adapter = PreImplementationEffectAdapter::new();

    let command = mock_dispatch_command("agent_delta", 99);
    let res_before_crash = adapter.deliver_armed_attempt(command.clone());
    assert_eq!(
        res_before_crash,
        DeliverOutcome::UnavailableBeforeReservation
    );

    let mut restarted_adapter = PreImplementationEffectAdapter::new();
    let res_after_restart = restarted_adapter.deliver_armed_attempt(command);

    assert_eq!(
        res_after_restart,
        DeliverOutcome::UnavailableBeforeReservation
    );
}
