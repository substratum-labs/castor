use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, DurabilityProfile,
    DurableStorage, EnsureRegionOutcome,
};
use castor_kernel::c04_adapter::{
    authority_binding_digest, DeliverOutcome, DispatchCommand, EffectAdapter, EffectProvider,
    ExternalKnowledge, ProviderOutcome,
};
use castor_kernel::castord::Castord;
use sha2::{Digest, Sha256};
use std::process::Command as ProcessCommand;
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

const ADAPTER_ID: &str = "payment_adapter";
const PROFILE: &str = "d1_at_most_one_submission";
const ACTION_BYTES: &[u8] = b"vertical-payment";

#[derive(Clone)]
struct CountingProvider {
    calls: Arc<AtomicUsize>,
    outcome: ProviderOutcome,
}

impl EffectProvider for CountingProvider {
    fn submit(&mut self, _command: &DispatchCommand) -> ProviderOutcome {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.outcome.clone()
    }
}

fn provider(outcome: ProviderOutcome) -> (CountingProvider, Arc<AtomicUsize>) {
    let calls = Arc::new(AtomicUsize::new(0));
    (
        CountingProvider {
            calls: calls.clone(),
            outcome,
        },
        calls,
    )
}

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

#[test]
fn castord_reopens_core_and_adapter_without_a_second_provider_submission() {
    let state = tempfile::tempdir().expect("state root");
    let (first_provider, first_calls) =
        provider(ProviderOutcome::Observed("receipt-vertical".to_string()));
    let mut castord = Castord::initialize(state.path(), ADAPTER_ID, PROFILE, first_provider)
        .expect("initialize castord");

    let action_digest = digest(ACTION_BYTES);
    assert!(matches!(
        castord.storage_mut().ensure_region(
            "region://agent-v/action-v",
            &action_digest,
            ACTION_BYTES,
            DurabilityProfile::D1,
        ),
        EnsureRegionOutcome::Success(_)
    ));
    let armed = match castord
        .storage_mut()
        .append_conditional(AppendConditionalRequest {
            agent_id: "agent-v".to_string(),
            entry_id: 1,
            expected_core_epoch: 1,
            expected_agent_generation: Some(1),
            expected_turn_id: Some(1),
            expected_lease_epoch: Some(1),
            expected_base_projection_digest: Some("projection-v0".to_string()),
            entry: CoreEntry::AttemptArmed {
                action_id: "action-v".to_string(),
                attempt_id: 1,
                action_region_ref: "region://agent-v/action-v".to_string(),
                action_digest: action_digest.clone(),
                request_digest: "request-v".to_string(),
            },
            region_refs: vec!["region://agent-v/action-v".to_string()],
        }) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("arm must persist: {other:?}"),
    };
    let dispatch = match castord
        .storage_mut()
        .append_conditional(AppendConditionalRequest {
            agent_id: "agent-v".to_string(),
            entry_id: 2,
            expected_core_epoch: 1,
            expected_agent_generation: Some(1),
            expected_turn_id: Some(1),
            expected_lease_epoch: Some(1),
            expected_base_projection_digest: Some(armed.entry_digest.clone()),
            entry: CoreEntry::DispatchAttempt {
                action_id: "action-v".to_string(),
                attempt_id: 1,
                adapter_id: ADAPTER_ID.to_string(),
            },
            region_refs: vec![],
        }) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("dispatch must persist: {other:?}"),
    };
    let mut command = DispatchCommand {
        agent_id: "agent-v".to_string(),
        action_id: "action-v".to_string(),
        attempt_id: 1,
        action_region_ref: "region://agent-v/action-v".to_string(),
        action_digest,
        request_digest: "request-v".to_string(),
        adapter_id: ADAPTER_ID.to_string(),
        assurance_profile: PROFILE.to_string(),
        attempt_armed_proof: armed,
        dispatch_proof: dispatch,
        authority_binding_digest: String::new(),
    };
    command.authority_binding_digest = authority_binding_digest(&command);
    assert_eq!(
        castord
            .effect_adapter_mut()
            .deliver_armed_attempt(command.clone()),
        DeliverOutcome::SubmissionObserved {
            accepted_and_durable: true,
            external_knowledge: ExternalKnowledge::Observed,
        }
    );
    assert_eq!(first_calls.load(Ordering::SeqCst), 1);
    drop(castord);

    let (recovery_provider, recovery_calls) = provider(ProviderOutcome::Unknown);
    let mut recovered = Castord::open(state.path(), ADAPTER_ID, PROFILE, recovery_provider)
        .expect("reopen castord");
    assert_eq!(
        recovered
            .effect_adapter_mut()
            .deliver_armed_attempt(command),
        DeliverOutcome::DuplicateDelivery {
            accepted_and_durable: true,
            prior_external_knowledge: ExternalKnowledge::Observed,
        }
    );
    assert_eq!(recovery_calls.load(Ordering::SeqCst), 0);

    let check = ProcessCommand::new(env!("CARGO_BIN_EXE_castord"))
        .args(["--state-dir", state.path().to_str().unwrap(), "--check"])
        .output()
        .expect("run castord check mode");
    assert!(
        check.status.success(),
        "castord --check failed: {}",
        String::from_utf8_lossy(&check.stderr)
    );
    assert!(String::from_utf8_lossy(&check.stdout).contains("castord state valid"));
}

#[test]
fn castord_check_on_missing_state_does_not_create_it() {
    let parent = tempfile::tempdir().expect("parent root");
    let missing = parent.path().join("missing-state");
    let check = ProcessCommand::new(env!("CARGO_BIN_EXE_castord"))
        .args(["--state-dir", missing.to_str().unwrap(), "--check"])
        .output()
        .expect("run castord check mode");

    assert!(!check.status.success());
    assert!(!missing.exists());
}
