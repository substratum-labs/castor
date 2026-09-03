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
    EffectObservationReport, EffectProvider, ExternalKnowledge, ProviderOutcome,
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

struct PanicProvider;

impl EffectProvider for PanicProvider {
    fn submit(&mut self, _command: &DispatchCommand) -> ProviderOutcome {
        panic!("injected provider crash after durable reservation")
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
            action_region_ref: action_region.clone(),
            action_digest: action_digest.clone(),
            request_digest: "request_hash_001".to_string(),
        },
        region_refs: vec![action_region.clone()],
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
        action_region_ref: action_region,
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

fn persisted_dispatch_command_with_decoy_region(core_root: &Path) -> DispatchCommand {
    let mut storage = D1DurableStorage::open(core_root).expect("open core D1 store");
    let action_digest = digest(ACTION_BYTES);
    let decoy_bytes = b"unrelated-observation";
    let decoy_digest = digest(decoy_bytes);
    assert!(matches!(
        storage.ensure_region(
            "region://agent_kappa/action",
            &action_digest,
            ACTION_BYTES,
            DurabilityProfile::D1,
        ),
        EnsureRegionOutcome::Success(_)
    ));
    assert!(matches!(
        storage.ensure_region(
            "region://agent_kappa/decoy",
            &decoy_digest,
            decoy_bytes,
            DurabilityProfile::D1,
        ),
        EnsureRegionOutcome::Success(_)
    ));
    let armed_proof = match storage.append_conditional(AppendConditionalRequest {
        agent_id: "agent_kappa".to_string(),
        entry_id: 10,
        expected_core_epoch: 1,
        expected_agent_generation: Some(2),
        expected_turn_id: Some(5),
        expected_lease_epoch: Some(3),
        expected_base_projection_digest: Some("projection-kappa".to_string()),
        entry: CoreEntry::AttemptArmed {
            action_id: "action_payment_01".to_string(),
            attempt_id: 5,
            action_region_ref: "region://agent_kappa/action".to_string(),
            action_digest: action_digest.clone(),
            request_digest: "request-kappa".to_string(),
        },
        region_refs: vec![
            "region://agent_kappa/action".to_string(),
            "region://agent_kappa/decoy".to_string(),
        ],
    }) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("AttemptArmed must persist: {other:?}"),
    };
    let dispatch_proof = match storage.append_conditional(AppendConditionalRequest {
        agent_id: "agent_kappa".to_string(),
        entry_id: 11,
        expected_core_epoch: 1,
        expected_agent_generation: Some(2),
        expected_turn_id: Some(5),
        expected_lease_epoch: Some(3),
        expected_base_projection_digest: Some(armed_proof.entry_digest.clone()),
        entry: CoreEntry::DispatchAttempt {
            action_id: "action_payment_01".to_string(),
            attempt_id: 5,
            adapter_id: ADAPTER_ID.to_string(),
        },
        region_refs: vec![],
    }) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("DispatchAttempt must persist: {other:?}"),
    };
    let mut command = DispatchCommand {
        agent_id: "agent_kappa".to_string(),
        action_id: "action_payment_01".to_string(),
        attempt_id: 5,
        action_region_ref: "region://agent_kappa/action".to_string(),
        action_digest: decoy_digest,
        request_digest: "request-kappa".to_string(),
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
    let command = persisted_dispatch_command(core.path(), "agent_alpha", "action_payment_01", 1);

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
    let command = persisted_dispatch_command(core.path(), "agent_beta", "action_payment_01", 42);

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
        entry: CoreEntry::TurnCommitted {
            turn_id: 105,
            successor_projection_digest: None,
            action_manifest_digest: None,
            action_manifest: vec![],
        },
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
    let (provider, submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut before_crash = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter D1 store");
    let command = persisted_dispatch_command(core.path(), "agent_delta", "action_payment_01", 99);

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

#[test]
fn hostile_trace_tampered_command_is_rejected_before_provider_io() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter D1 store");
    let mut command =
        persisted_dispatch_command(core.path(), "agent_epsilon", "action_payment_01", 7);
    command.request_digest = "tampered-request".to_string();
    command.authority_binding_digest = authority_binding_digest(&command);

    assert!(matches!(
        effect_adapter.deliver_armed_attempt(command),
        DeliverOutcome::RejectedInvalidCommand(_)
    ));
    assert_eq!(submissions.load(Ordering::SeqCst), 0);
}

#[test]
fn hostile_trace_same_attempt_number_for_two_agents_does_not_collide() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, submissions) =
        ScriptedProvider::new(ProviderOutcome::Observed("provider-receipt".to_string()));
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter D1 store");
    let first = persisted_dispatch_command(core.path(), "agent_zeta", "action_payment_01", 1);
    let second = persisted_dispatch_command(core.path(), "agent_eta", "action_payment_01", 1);

    assert!(matches!(
        effect_adapter.deliver_armed_attempt(first),
        DeliverOutcome::SubmissionObserved { .. }
    ));
    assert!(matches!(
        effect_adapter.deliver_armed_attempt(second),
        DeliverOutcome::SubmissionObserved { .. }
    ));
    assert_eq!(submissions.load(Ordering::SeqCst), 2);
}

#[test]
fn hostile_trace_deleted_adapter_journal_fails_closed_on_recovery() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, _) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter D1 store");
    let command = persisted_dispatch_command(core.path(), "agent_theta", "action_payment_01", 3);
    assert!(matches!(
        effect_adapter.deliver_armed_attempt(command),
        DeliverOutcome::SubmissionObserved { .. }
    ));
    drop(effect_adapter);
    std::fs::remove_file(adapter.path().join("adapter-journal.jsonl"))
        .expect("simulate lost adapter journal");

    let (recovery_provider, recovery_submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    assert!(D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        recovery_provider,
    )
    .is_err());
    assert_eq!(recovery_submissions.load(Ordering::SeqCst), 0);
}

#[test]
fn hostile_trace_adapter_cannot_initialize_after_core_dispatch_exists() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let _command = persisted_dispatch_command(core.path(), "agent_iota", "action_payment_01", 4);
    let (provider, submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);

    assert!(D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .is_err());
    assert_eq!(submissions.load(Ordering::SeqCst), 0);
}

#[test]
fn hostile_trace_two_live_adapter_instances_cannot_share_one_root() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (first_provider, _) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let first = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        first_provider,
    )
    .expect("first adapter owns root");
    let (second_provider, second_submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);

    assert!(D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        second_provider,
    )
    .is_err());
    assert_eq!(second_submissions.load(Ordering::SeqCst), 0);

    drop(first);
    let (recovery_provider, _) = ScriptedProvider::new(ProviderOutcome::Unknown);
    D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        recovery_provider,
    )
    .expect("adapter ownership is released on drop");
}

#[test]
fn hostile_trace_non_action_region_digest_cannot_authorize_provider_io() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter before Core dispatch");
    let command = persisted_dispatch_command_with_decoy_region(core.path());

    assert!(matches!(
        effect_adapter.deliver_armed_attempt(command),
        DeliverOutcome::RejectedInvalidCommand(_)
    ));
    assert_eq!(submissions.load(Ordering::SeqCst), 0);
}

#[test]
fn hostile_trace_conflicting_observations_are_both_retained_across_reopen() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, _) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter before Core dispatch");
    let command = persisted_dispatch_command(core.path(), "agent_lambda", "action_payment_01", 8);
    assert!(matches!(
        effect_adapter.deliver_armed_attempt(command),
        DeliverOutcome::SubmissionObserved { .. }
    ));
    let first = EffectObservationReport {
        agent_id: "agent_lambda".to_string(),
        action_id: "action_payment_01".to_string(),
        attempt_id: 8,
        observation_id: "provider-status-1".to_string(),
        observation_digest: "sha256:observed-success".to_string(),
        adapter_id: ADAPTER_ID.to_string(),
        evidence_ref: Some("evidence://success".to_string()),
    };
    let conflicting = EffectObservationReport {
        observation_digest: "sha256:observed-failure".to_string(),
        evidence_ref: Some("evidence://failure".to_string()),
        ..first.clone()
    };

    assert!(effect_adapter.report_effect_observation(first.clone()));
    assert!(effect_adapter.report_effect_observation(first.clone()));
    assert!(effect_adapter.report_effect_observation(conflicting.clone()));
    drop(effect_adapter);

    let (recovery_provider, _) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let recovered = D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        recovery_provider,
    )
    .expect("reopen adapter with conflicting observations");
    let reports = recovered.observation_reports(
        "agent_lambda",
        "action_payment_01",
        8,
        ADAPTER_ID,
        "provider-status-1",
    );
    assert_eq!(reports.len(), 2);
    assert!(reports.contains(&first));
    assert!(reports.contains(&conflicting));
}

#[test]
fn hostile_trace_consistent_adapter_genesis_rollback_is_blocked_by_core_anchor() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, first_submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter before Core dispatch");
    let genesis_journal =
        std::fs::read(adapter.path().join("adapter-journal.jsonl")).expect("read genesis journal");
    let genesis_head =
        std::fs::read(adapter.path().join("adapter-head.json")).expect("read genesis head");
    let command = persisted_dispatch_command(core.path(), "agent_mu", "action_payment_01", 9);
    assert!(matches!(
        effect_adapter.deliver_armed_attempt(command.clone()),
        DeliverOutcome::SubmissionObserved { .. }
    ));
    assert_eq!(first_submissions.load(Ordering::SeqCst), 1);
    drop(effect_adapter);

    std::fs::write(
        adapter.path().join("adapter-journal.jsonl"),
        genesis_journal,
    )
    .expect("restore coherent empty journal");
    std::fs::write(adapter.path().join("adapter-head.json"), genesis_head)
        .expect("restore coherent genesis head");
    let (recovery_provider, recovery_submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut recovered = D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        recovery_provider,
    )
    .expect("coherently rolled-back adapter store opens");

    assert_eq!(
        recovered.deliver_armed_attempt(command),
        DeliverOutcome::Ambiguous {
            accepted_and_durable: true,
        }
    );
    assert_eq!(recovery_submissions.load(Ordering::SeqCst), 0);
}

#[test]
fn hostile_trace_adapter_lineage_cannot_be_reinitialized_after_store_loss() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, _) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize first lineage");
    drop(effect_adapter);
    std::fs::remove_dir_all(adapter.path()).expect("simulate complete adapter store loss");
    std::fs::create_dir(adapter.path()).expect("recreate empty adapter root");

    let (replacement_provider, replacement_submissions) =
        ScriptedProvider::new(ProviderOutcome::Unknown);
    assert!(D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        replacement_provider,
    )
    .is_err());
    assert_eq!(replacement_submissions.load(Ordering::SeqCst), 0);
}

#[test]
fn hostile_trace_provider_panic_after_reservation_recovers_without_resubmit() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let mut effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        PanicProvider,
    )
    .expect("initialize adapter before Core dispatch");
    let command = persisted_dispatch_command(core.path(), "agent_nu", "action_payment_01", 10);

    assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        effect_adapter.deliver_armed_attempt(command.clone())
    }))
    .is_err());
    drop(effect_adapter);

    let (recovery_provider, recovery_submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let mut recovered = D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        recovery_provider,
    )
    .expect("reopen Reserved-only adapter state");
    assert_eq!(
        recovered.deliver_armed_attempt(command),
        DeliverOutcome::DuplicateDelivery {
            accepted_and_durable: true,
            prior_external_knowledge: ExternalKnowledge::Unknown,
        }
    );
    assert_eq!(recovery_submissions.load(Ordering::SeqCst), 0);
}

#[test]
fn hostile_trace_truncated_adapter_journal_fails_closed() {
    let core = tempfile::tempdir().expect("core root");
    let adapter = tempfile::tempdir().expect("adapter root");
    let (provider, _) = ScriptedProvider::new(ProviderOutcome::Unknown);
    let effect_adapter = D1EffectAdapter::initialize(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        provider,
    )
    .expect("initialize adapter");
    drop(effect_adapter);
    use std::io::Write;
    let mut journal = std::fs::OpenOptions::new()
        .append(true)
        .open(adapter.path().join("adapter-journal.jsonl"))
        .expect("open adapter journal");
    journal.write_all(b"{truncated").expect("write torn tail");
    journal.sync_all().expect("persist torn tail");
    drop(journal);

    let (recovery_provider, recovery_submissions) = ScriptedProvider::new(ProviderOutcome::Unknown);
    assert!(D1EffectAdapter::open(
        adapter.path(),
        core.path(),
        ADAPTER_ID,
        ASSURANCE_PROFILE,
        recovery_provider,
    )
    .is_err());
    assert_eq!(recovery_submissions.load(Ordering::SeqCst), 0);
}
