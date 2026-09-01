//! Hostile D1 harness for C-01 Durable Storage and C-04 Effect Adapter.
//!
//! The tests use real persisted C-01 proofs, explicit filesystem roots and a
//! scripted provider. No mock proof, global adapter state or provider network is
//! allowed to stand in for the D1 linearization points.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use castor_kernel::c04_adapter::{
    authority_binding_digest, D1EffectAdapter, DeliverOutcome, DispatchCommand, EffectAdapter,
    EffectProvider, ExternalKnowledge, ProviderOutcome,
};
use sha2::{Digest, Sha256};
use std::path::Path;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

const ADAPTER_ID: &str = "http_payment_adapter";
const ASSURANCE_PROFILE: &str = "at_least_once";
const ACTION_BYTES: &[u8] = b"pay-invoice-42";

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

#[derive(Clone)]
struct ScriptedProvider {
    outcome: ProviderOutcome,
    submissions: Arc<AtomicUsize>,
}

impl ScriptedProvider {
    fn new(outcome: ProviderOutcome) -> (Self, Arc<AtomicUsize>) {
        let submissions = Arc::new(AtomicUsize::new(0));
        (
            Self {
                outcome,
                submissions: submissions.clone(),
            },
            submissions,
        )
    }
}

impl EffectProvider for ScriptedProvider {
    fn submit(&mut self, _command: &DispatchCommand) -> ProviderOutcome {
        self.submissions.fetch_add(1, Ordering::SeqCst);
        self.outcome.clone()
    }
}

fn persisted_dispatch_command(
    core_root: &Path,
    agent_id: &str,
    action_id: &str,
    attempt_id: u64,
) -> DispatchCommand {
    let mut storage = D1DurableStorage::open(core_root).expect("open core D1 store");
    let action_digest = digest(ACTION_BYTES);
    let action_region = format!("region://{agent_id}/{action_id}");

    assert!(matches!(
        storage.ensure_region(
            &action_region,
            &action_digest,
            ACTION_BYTES,
            DurabilityProfile::D1,
        ),
        EnsureRegionOutcome::Success(_) | EnsureRegionOutcome::AlreadyPersistedSameContent(_)
    ));

    let armed_request = AppendConditionalRequest {
        agent_id: agent_id.to_string(),
        entry_id: 10,
        expected_core_epoch: 1,
        expected_agent_generation: Some(2),
        expected_turn_id: Some(5),
        expected_lease_epoch: Some(3),
        expected_base_projection_digest: Some("base_projection_hash_001".to_string()),
        entry: CoreEntry::AttemptArmed {
            action_id: action_id.to_string(),
            attempt_id,
            request_digest: "request_hash_001".to_string(),
        },
        region_refs: vec![action_region],
    };
    let armed_proof = match storage.append_conditional(armed_request) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("AttemptArmed must persist, got {other:?}"),
    };

    let dispatch_request = AppendConditionalRequest {
        agent_id: agent_id.to_string(),
        entry_id: 11,
        expected_core_epoch: 1,
        expected_agent_generation: Some(2),
        expected_turn_id: Some(5),
        expected_lease_epoch: Some(3),
        expected_base_projection_digest: Some(armed_proof.entry_digest.clone()),
        entry: CoreEntry::DispatchAttempt {
            action_id: action_id.to_string(),
            attempt_id,
            adapter_id: ADAPTER_ID.to_string(),
        },
        region_refs: vec![],
    };
    let dispatch_proof = match storage.append_conditional(dispatch_request) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("DispatchAttempt must persist, got {other:?}"),
    };

    let mut command = DispatchCommand {
        agent_id: agent_id.to_string(),
        action_id: action_id.to_string(),
        attempt_id,
        action_digest,
        request_digest: "request_hash_001".to_string(),
        adapter_id: ADAPTER_ID.to_string(),
        assurance_profile: ASSURANCE_PROFILE.to_string(),
        attempt_armed_proof: armed_proof,
        dispatch_proof,
        authority_binding_digest: String::new(),
    };
    command.authority_binding_digest = authority_binding_digest(&command);
    command
}

#[test]
fn hostile_trace_normal_delivery_uses_real_proofs_and_one_provider_submission() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let command = persisted_dispatch_command(core.path(), "agent_alpha", "action_payment_01", 1);
    let (provider, submissions) =
        ScriptedProvider::new(ProviderOutcome::Observed("provider-receipt-1".to_string()));
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter D1 store");

    assert_eq!(
        effect_adapter.deliver_armed_attempt(command),
        DeliverOutcome::SubmissionObserved {
            accepted_and_durable: true,
            external_knowledge: ExternalKnowledge::Observed,
        }
    );
    assert_eq!(submissions.load(Ordering::SeqCst), 1);
}

#[test]
fn hostile_trace_duplicate_delivery_reuses_durable_dedup_record() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let command = persisted_dispatch_command(core.path(), "agent_beta", "action_payment_01", 42);
    let (provider, submissions) =
        ScriptedProvider::new(ProviderOutcome::Observed("provider-receipt-42".to_string()));
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter D1 store");

    assert!(matches!(
        effect_adapter.deliver_armed_attempt(command.clone()),
        DeliverOutcome::SubmissionObserved { .. }
    ));
    assert_eq!(
        effect_adapter.deliver_armed_attempt(command),
        DeliverOutcome::DuplicateDelivery {
            accepted_and_durable: true,
            prior_external_knowledge: ExternalKnowledge::Observed,
        }
    );
    assert_eq!(submissions.load(Ordering::SeqCst), 1);
}

#[test]
fn hostile_trace_lost_acknowledgement_recovers_same_entry_from_disk() {
    let core = tempfile::tempdir().expect("core root");
    let request = AppendConditionalRequest {
        agent_id: "agent_gamma".to_string(),
        entry_id: 200,
        expected_core_epoch: 1,
        expected_agent_generation: Some(1),
        expected_turn_id: Some(105),
        expected_lease_epoch: Some(1),
        expected_base_projection_digest: Some("proj_hash_200".to_string()),
        entry: CoreEntry::TurnCommitted { turn_id: 105 },
        region_refs: vec![],
    };
    let mut storage = D1DurableStorage::open(core.path()).expect("open core store");
    let proof = match storage.append_conditional(request.clone()) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("first append must persist, got {other:?}"),
    };
    drop(storage);

    let mut recovered = D1DurableStorage::open(core.path()).expect("recover core store");
    assert_eq!(
        recovered.append_conditional(request.clone()),
        AppendConditionalOutcome::AlreadyPersistedSameEntry(proof.clone())
    );
    assert_eq!(
        recovered.read_entry(&request.agent_id, request.entry_id),
        Some(proof)
    );
}

#[test]
fn hostile_trace_crash_after_possible_submit_never_calls_provider_twice() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let command = persisted_dispatch_command(core.path(), "agent_delta", "action_payment_01", 99);
    let (provider, submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut before_crash = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter D1 store");

    assert_eq!(
        before_crash.deliver_armed_attempt(command.clone()),
        DeliverOutcome::SubmissionObserved {
            accepted_and_durable: true,
            external_knowledge: ExternalKnowledge::Unknown,
        }
    );
    assert_eq!(submissions.load(Ordering::SeqCst), 1);
    drop(before_crash);

    let (recovery_provider, recovery_submissions) =
        ScriptedProvider::new(ProviderOutcome::Observed("must-not-submit".to_string()));
    let mut after_crash = D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        recovery_provider,
    )
    .expect("reopen adapter D1 store");
    assert_eq!(
        after_crash.deliver_armed_attempt(command),
        DeliverOutcome::DuplicateDelivery {
            accepted_and_durable: true,
            prior_external_knowledge: ExternalKnowledge::Unknown,
        }
    );
    assert_eq!(recovery_submissions.load(Ordering::SeqCst), 0);
    assert_eq!(submissions.load(Ordering::SeqCst), 1);
}
