//! Single-store D1 governed-turn composition.

use crate::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage, DurableStorage,
};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::io;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GovernedTurnOutcome {
    Admitted,
    InteractionRequested,
    InteractionBound,
    InteractionConsumed,
    TurnCommitted,
    ActionRegistered,
    AttemptArmed { attempt_id: u64 },
    DispatchRecorded,
    Delivered,
    DuplicateDelivery,
    Settled { resolution: String },
    QuarantinedDispute,
    GenerationFenced { generation: u64 },
    CapabilityRevoked,
    Reconstructed,
    Ambiguous,
    RejectedStaleAuthority,
    RejectedStaleGeneration { current_generation: u64 },
    RejectedCapabilityRevoked,
    RejectedInvalidProofClass,
    RejectedLateOrClosedTurn,
    RejectedCurrentState,
    RejectedPrecondition,
    IntegrityOrProtocolFault,
    UnavailableBeforeAck,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmitTurnRequest {
    pub agent_id: String,
    pub turn_id: u64,
    pub lease_epoch: u64,
    pub base_projection_digest: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestInteractionRequest {
    pub interaction_id: String,
    pub lease_epoch: u64,
    pub request_digest: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InteractionOutcomeReport {
    pub interaction_id: String,
    pub observation_region_id: String,
    pub observation_digest: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConsumeInteractionRequest {
    pub interaction_id: String,
    pub lease_epoch: u64,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommitTurnRequest {
    pub lease_epoch: u64,
    pub base_projection_digest: String,
    pub successor_region_id: String,
    pub successor_digest: String,
    pub action_manifest_region_id: String,
    pub action_manifest_digest: String,
    pub action_manifest: Vec<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionRegistrationRequest {
    pub action_id: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PresentAdmissionCertificateRequest {
    pub action_id: String,
    pub target_scope: String,
    pub capability_id: String,
    pub generation: u64,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecordDispatchAttemptRequest {
    pub attempt_id: u64,
    pub dispatch_identity: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeliverArmedAttemptRequest {
    pub attempt_id: u64,
    pub dispatch_identity: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PresentSettlementCertificateRequest {
    pub attempt_id: u64,
    pub dispatch_identity: String,
    pub evidence_region_id: String,
    pub evidence_digest: String,
    pub resolution: String,
    pub proof_class: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TurnStatus {
    Ready,
    AwaitingInteraction,
    Closed,
}
#[derive(Clone, Debug)]
struct Turn {
    turn_id: u64,
    base_digest: String,
    last_lease: u64,
    active_lease: Option<u64>,
    status: TurnStatus,
    requested: HashSet<String>,
    interactions: HashMap<String, String>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AttemptStatus {
    ArmedUnknown,
    Dispatched,
    Settled,
    QuarantinedDispute,
}
#[derive(Clone, Debug)]
struct Attempt {
    action_id: String,
    target_scope: String,
    status: AttemptStatus,
    dispatch_identity: Option<String>,
    delivered: bool,
    ambiguous_delivery: bool,
    reserved_delivery: bool,
    settlement: Option<(String, String, String, String)>,
}

/// The only semantic writer. The C-01 handle is never shared mutably with a
/// slice; all journal entries originate in this projection.
pub struct D1GovernedTurnAuthority {
    storage: Option<D1DurableStorage>,
    root: PathBuf,
    generation: u64,
    agent_id: Option<String>,
    projection_digest: Option<String>,
    turn: Option<Turn>,
    committed_actions: HashMap<String, (String, String)>,
    registered_actions: HashSet<String>,
    attempts: HashMap<u64, Attempt>,
    revoked_capabilities: HashSet<String>,
    next_entry_id: u64,
    next_attempt_id: u64,
    provider_submissions: u64,
}

impl D1GovernedTurnAuthority {
    pub fn open(path: impl AsRef<Path>) -> io::Result<Self> {
        D1DurableStorage::open(path).map(Self::for_test)
    }
    pub fn for_test(storage: D1DurableStorage) -> Self {
        let root = storage.root().to_path_buf();
        let mut authority = Self {
            storage: Some(storage),
            root,
            generation: 1,
            agent_id: None,
            projection_digest: None,
            turn: None,
            committed_actions: HashMap::new(),
            registered_actions: HashSet::new(),
            attempts: HashMap::new(),
            revoked_capabilities: HashSet::new(),
            next_entry_id: 1,
            next_attempt_id: 1,
            provider_submissions: 0,
        };
        authority.replay();
        authority
    }
    pub fn storage(&self) -> &D1DurableStorage {
        self.storage.as_ref().expect("storage present")
    }
    pub fn provider_submission_count(&self) -> u64 {
        self.provider_submissions
    }

    fn storage_mut(&mut self) -> &mut D1DurableStorage {
        self.storage.as_mut().expect("storage present")
    }
    fn append(
        &mut self,
        entry: CoreEntry,
        regions: Vec<String>,
        turn_id: Option<u64>,
        lease: Option<u64>,
        base: Option<String>,
    ) -> Result<(), GovernedTurnOutcome> {
        let Some(agent_id) = self.agent_id.clone() else {
            return Err(GovernedTurnOutcome::RejectedPrecondition);
        };
        let updates_projection = matches!(
            &entry,
            CoreEntry::AttemptArmed { .. }
                | CoreEntry::DispatchAttempt { .. }
                | CoreEntry::AttemptSettled { .. }
                | CoreEntry::QuarantinedDispute { .. }
                | CoreEntry::AdapterReservation { .. }
                | CoreEntry::AdapterSubmissionRecorded { .. }
        );
        let request = AppendConditionalRequest {
            agent_id,
            entry_id: self.next_entry_id,
            expected_core_epoch: 1,
            expected_agent_generation: Some(self.generation),
            expected_turn_id: turn_id,
            expected_lease_epoch: lease,
            expected_base_projection_digest: base,
            entry,
            region_refs: regions,
        };
        match self.storage_mut().append_conditional(request) {
            AppendConditionalOutcome::EntryPersisted(proof)
            | AppendConditionalOutcome::AlreadyPersistedSameEntry(proof) => {
                self.next_entry_id = proof.entry_id + 1;
                if updates_projection {
                    self.projection_digest = Some(proof.entry_digest);
                }
                Ok(())
            }
            AppendConditionalOutcome::RejectedPrecondition { .. } => {
                Err(GovernedTurnOutcome::RejectedStaleAuthority)
            }
            AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion
            | AppendConditionalOutcome::IntegrityFault => {
                Err(GovernedTurnOutcome::IntegrityOrProtocolFault)
            }
            AppendConditionalOutcome::UnavailableBeforeAck => {
                Err(GovernedTurnOutcome::UnavailableBeforeAck)
            }
        }
    }
    fn region_matches(&self, id: &str, digest: &str) -> bool {
        self.storage()
            .read_region(id)
            .is_some_and(|bytes| format!("sha256:{:x}", Sha256::digest(bytes)) == digest)
    }
    fn scope_locked(&self, scope: &str) -> bool {
        self.attempts.values().any(|attempt| {
            attempt.target_scope == scope
                && matches!(
                    attempt.status,
                    AttemptStatus::ArmedUnknown
                        | AttemptStatus::Dispatched
                        | AttemptStatus::QuarantinedDispute
                )
        })
    }
    fn replay(&mut self) {
        let requests = self.storage().journal_requests();
        self.generation = 1;
        self.agent_id = None;
        self.projection_digest = None;
        self.turn = None;
        self.committed_actions.clear();
        self.registered_actions.clear();
        self.attempts.clear();
        self.revoked_capabilities.clear();
        self.next_entry_id = 1;
        self.next_attempt_id = 1;
        self.provider_submissions = 0;
        for request in requests {
            let proof_digest = self
                .storage()
                .read_entry(&request.agent_id, request.entry_id)
                .map(|proof| proof.entry_digest);
            self.agent_id = Some(request.agent_id.clone());
            self.next_entry_id = self.next_entry_id.max(request.entry_id + 1);
            match request.entry {
                CoreEntry::LeaseGranted {
                    turn_id,
                    lease_epoch,
                } => {
                    if let Some(turn) = self
                        .turn
                        .as_mut()
                        .filter(|turn| turn.turn_id == turn_id && !turn.interactions.is_empty())
                    {
                        turn.last_lease = lease_epoch;
                        turn.active_lease = Some(lease_epoch);
                        turn.status = TurnStatus::Ready;
                    } else {
                        let base = request.expected_base_projection_digest.unwrap_or_default();
                        self.projection_digest = Some(base.clone());
                        self.turn = Some(Turn {
                            turn_id,
                            base_digest: base,
                            last_lease: lease_epoch,
                            active_lease: Some(lease_epoch),
                            status: TurnStatus::Ready,
                            requested: HashSet::new(),
                            interactions: HashMap::new(),
                        });
                    }
                }
                CoreEntry::InteractionRequested {
                    turn_id,
                    interaction_id,
                    ..
                } => {
                    if let Some(turn) = self.turn.as_mut() {
                        if turn.turn_id == turn_id {
                            turn.active_lease = None;
                            turn.status = TurnStatus::AwaitingInteraction;
                            turn.requested.insert(interaction_id);
                        }
                    }
                }
                CoreEntry::InteractionBound {
                    turn_id,
                    interaction_id,
                    region_id,
                    ..
                } => {
                    if let Some(turn) = self.turn.as_mut() {
                        if turn.turn_id == turn_id {
                            turn.interactions.insert(interaction_id, region_id);
                            turn.status = TurnStatus::Ready;
                        }
                    }
                }
                CoreEntry::TurnCommitted {
                    successor_projection_digest,
                    action_manifest,
                    action_manifest_digest,
                    ..
                } => {
                    self.projection_digest = successor_projection_digest;
                    if let Some(turn) = self.turn.as_mut() {
                        turn.active_lease = None;
                        turn.status = TurnStatus::Closed;
                    }
                    if let Some(digest) = action_manifest_digest {
                        let region = request.region_refs.get(1).cloned().unwrap_or_default();
                        for action in action_manifest {
                            self.registered_actions.insert(action.clone());
                            self.committed_actions
                                .insert(action, (region.clone(), digest.clone()));
                        }
                    }
                }
                CoreEntry::AttemptArmed {
                    action_id,
                    attempt_id,
                    request_digest,
                    ..
                } => {
                    self.attempts.insert(
                        attempt_id,
                        Attempt {
                            action_id,
                            target_scope: request_digest,
                            status: AttemptStatus::ArmedUnknown,
                            dispatch_identity: None,
                            delivered: false,
                            ambiguous_delivery: false,
                            reserved_delivery: false,
                            settlement: None,
                        },
                    );
                    self.next_attempt_id = self.next_attempt_id.max(attempt_id + 1);
                    self.projection_digest = proof_digest;
                }
                CoreEntry::DispatchAttempt {
                    attempt_id,
                    adapter_id,
                    ..
                } => {
                    if let Some(attempt) = self.attempts.get_mut(&attempt_id) {
                        attempt.status = AttemptStatus::Dispatched;
                        attempt.dispatch_identity = Some(adapter_id);
                        attempt.ambiguous_delivery = true;
                    }
                    self.projection_digest = proof_digest;
                }
                CoreEntry::AdapterReservation { attempt_id } => {
                    if let Some(attempt) = self.attempts.get_mut(&attempt_id) {
                        attempt.reserved_delivery = true;
                        attempt.ambiguous_delivery = true;
                    }
                    self.projection_digest = proof_digest;
                }
                CoreEntry::AdapterSubmissionRecorded { attempt_id } => {
                    if let Some(attempt) = self.attempts.get_mut(&attempt_id) {
                        attempt.delivered = true;
                        attempt.ambiguous_delivery = false;
                    }
                    self.projection_digest = proof_digest;
                }
                CoreEntry::AttemptSettled {
                    attempt_id,
                    resolution,
                    evidence_region_id,
                    evidence_digest,
                    ..
                } => {
                    if let Some(attempt) = self.attempts.get_mut(&attempt_id) {
                        attempt.status = AttemptStatus::Settled;
                        attempt.settlement = Some((
                            resolution,
                            evidence_region_id,
                            evidence_digest,
                            attempt.dispatch_identity.clone().unwrap_or_default(),
                        ));
                    }
                    self.projection_digest = proof_digest;
                }
                CoreEntry::QuarantinedDispute { attempt_id, .. } => {
                    if let Some(attempt) = self.attempts.get_mut(&attempt_id) {
                        attempt.status = AttemptStatus::QuarantinedDispute;
                    }
                    self.projection_digest = proof_digest;
                }
                CoreEntry::FenceRevoked { generation } => {
                    self.generation = generation;
                    if let Some(turn) = self.turn.as_mut() {
                        turn.active_lease = None;
                        turn.status = TurnStatus::Closed;
                    }
                }
                CoreEntry::CapabilityRevoked { capability_id } => {
                    self.revoked_capabilities.insert(capability_id);
                }
                CoreEntry::ConflictingInteractionOutcomeAppended { .. }
                | CoreEntry::InteractionTurnClosed { .. } => {}
            }
        }
    }

    pub fn admit_turn(&mut self, request: AdmitTurnRequest) -> GovernedTurnOutcome {
        if self
            .turn
            .as_ref()
            .is_some_and(|turn| turn.status != TurnStatus::Closed)
            || request.lease_epoch != 0
            || request.agent_id.is_empty()
            || self
                .agent_id
                .as_deref()
                .is_some_and(|agent| agent != request.agent_id)
        {
            return GovernedTurnOutcome::RejectedPrecondition;
        }
        self.agent_id = Some(request.agent_id.clone());
        self.projection_digest = Some(request.base_projection_digest.clone());
        if let Err(outcome) = self.append(
            CoreEntry::LeaseGranted {
                turn_id: request.turn_id,
                lease_epoch: request.lease_epoch,
            },
            vec![],
            None,
            None,
            Some(request.base_projection_digest.clone()),
        ) {
            return outcome;
        }
        self.turn = Some(Turn {
            turn_id: request.turn_id,
            base_digest: request.base_projection_digest,
            last_lease: request.lease_epoch,
            active_lease: Some(request.lease_epoch),
            status: TurnStatus::Ready,
            requested: HashSet::new(),
            interactions: HashMap::new(),
        });
        GovernedTurnOutcome::Admitted
    }
    pub fn request_interaction(
        &mut self,
        request: RequestInteractionRequest,
    ) -> GovernedTurnOutcome {
        let Some(turn) = self.turn.as_ref() else {
            return GovernedTurnOutcome::RejectedStaleAuthority;
        };
        if turn.status != TurnStatus::Ready || turn.active_lease != Some(request.lease_epoch) {
            return GovernedTurnOutcome::RejectedStaleAuthority;
        }
        let (id, base) = (turn.turn_id, turn.base_digest.clone());
        if let Err(outcome) = self.append(
            CoreEntry::InteractionRequested {
                turn_id: id,
                interaction_id: request.interaction_id.clone(),
                request_digest: request.request_digest,
                service_id: "D1".into(),
            },
            vec![],
            Some(id),
            Some(request.lease_epoch),
            Some(base),
        ) {
            return outcome;
        }
        let turn = self.turn.as_mut().expect("turn exists");
        turn.active_lease = None;
        turn.status = TurnStatus::AwaitingInteraction;
        turn.requested.insert(request.interaction_id);
        GovernedTurnOutcome::InteractionRequested
    }
    pub fn report_outcome(&mut self, report: InteractionOutcomeReport) -> GovernedTurnOutcome {
        let Some(turn) = self.turn.as_ref() else {
            return GovernedTurnOutcome::RejectedLateOrClosedTurn;
        };
        if turn.status == TurnStatus::Closed
            || !matches!(turn.status, TurnStatus::AwaitingInteraction)
            || !turn.requested.contains(&report.interaction_id)
        {
            return GovernedTurnOutcome::RejectedLateOrClosedTurn;
        }
        if !self.region_matches(&report.observation_region_id, &report.observation_digest) {
            return GovernedTurnOutcome::IntegrityOrProtocolFault;
        }
        let (id, base) = (turn.turn_id, turn.base_digest.clone());
        if let Err(outcome) = self.append(
            CoreEntry::InteractionBound {
                turn_id: id,
                interaction_id: report.interaction_id.clone(),
                region_id: report.observation_region_id.clone(),
                result_digest: report.observation_digest,
                disposition: "Bound".into(),
            },
            vec![report.observation_region_id.clone()],
            Some(id),
            None,
            Some(base),
        ) {
            return outcome;
        }
        let turn = self.turn.as_mut().expect("turn exists");
        turn.interactions
            .insert(report.interaction_id, report.observation_region_id);
        turn.status = TurnStatus::Ready;
        GovernedTurnOutcome::InteractionBound
    }
    pub fn consume_interaction(
        &mut self,
        request: ConsumeInteractionRequest,
    ) -> GovernedTurnOutcome {
        let Some(turn) = self.turn.as_ref() else {
            return GovernedTurnOutcome::RejectedStaleAuthority;
        };
        if turn.status != TurnStatus::Ready
            || !turn.interactions.contains_key(&request.interaction_id)
            || request.lease_epoch <= turn.last_lease
            || turn.active_lease.is_some()
        {
            return GovernedTurnOutcome::RejectedStaleAuthority;
        }
        let (id, base) = (turn.turn_id, turn.base_digest.clone());
        if let Err(outcome) = self.append(
            CoreEntry::LeaseGranted {
                turn_id: id,
                lease_epoch: request.lease_epoch,
            },
            vec![],
            Some(id),
            None,
            Some(base),
        ) {
            return outcome;
        }
        let turn = self.turn.as_mut().expect("turn exists");
        turn.last_lease = request.lease_epoch;
        turn.active_lease = Some(request.lease_epoch);
        GovernedTurnOutcome::InteractionConsumed
    }
    pub fn commit_turn(&mut self, request: CommitTurnRequest) -> GovernedTurnOutcome {
        let Some(turn) = self.turn.as_ref() else {
            return GovernedTurnOutcome::RejectedStaleAuthority;
        };
        if turn.status != TurnStatus::Ready
            || turn.active_lease != Some(request.lease_epoch)
            || turn.base_digest != request.base_projection_digest
            || turn.interactions.is_empty()
        {
            return GovernedTurnOutcome::RejectedStaleAuthority;
        }
        if !self.region_matches(&request.successor_region_id, &request.successor_digest)
            || !self.region_matches(
                &request.action_manifest_region_id,
                &request.action_manifest_digest,
            )
        {
            return GovernedTurnOutcome::IntegrityOrProtocolFault;
        }
        let Some(manifest_bytes) = self
            .storage()
            .read_region(&request.action_manifest_region_id)
        else {
            return GovernedTurnOutcome::IntegrityOrProtocolFault;
        };
        let Ok(manifest_text) = String::from_utf8(manifest_bytes) else {
            return GovernedTurnOutcome::IntegrityOrProtocolFault;
        };
        let manifest_actions: Vec<_> = manifest_text
            .lines()
            .filter(|line| !line.is_empty())
            .map(str::to_owned)
            .collect();
        if manifest_actions != request.action_manifest {
            return GovernedTurnOutcome::IntegrityOrProtocolFault;
        }
        let id = turn.turn_id;
        let manifest_region = request.action_manifest_region_id.clone();
        if let Err(outcome) = self.append(
            CoreEntry::TurnCommitted {
                turn_id: id,
                successor_projection_digest: Some(request.successor_digest.clone()),
                action_manifest_digest: Some(request.action_manifest_digest.clone()),
                action_manifest: request.action_manifest.clone(),
            },
            vec![request.successor_region_id, manifest_region],
            Some(id),
            Some(request.lease_epoch),
            Some(request.base_projection_digest),
        ) {
            return outcome;
        }
        self.projection_digest = Some(request.successor_digest);
        for action in request.action_manifest {
            self.committed_actions.insert(
                action,
                (
                    request.action_manifest_region_id.clone(),
                    request.action_manifest_digest.clone(),
                ),
            );
        }
        let turn = self.turn.as_mut().expect("turn exists");
        turn.active_lease = None;
        turn.status = TurnStatus::Closed;
        GovernedTurnOutcome::TurnCommitted
    }
    pub fn register_action(&mut self, request: ActionRegistrationRequest) -> GovernedTurnOutcome {
        if !self.committed_actions.contains_key(&request.action_id) {
            return GovernedTurnOutcome::RejectedPrecondition;
        }
        self.registered_actions.insert(request.action_id);
        GovernedTurnOutcome::ActionRegistered
    }
    pub fn present_admission_certificate(
        &mut self,
        request: PresentAdmissionCertificateRequest,
    ) -> GovernedTurnOutcome {
        if !self.registered_actions.contains(&request.action_id) {
            return GovernedTurnOutcome::RejectedPrecondition;
        }
        if request.generation != self.generation {
            return GovernedTurnOutcome::RejectedStaleGeneration {
                current_generation: self.generation,
            };
        }
        if self.revoked_capabilities.contains(&request.capability_id) {
            return GovernedTurnOutcome::RejectedCapabilityRevoked;
        }
        if self.scope_locked(&request.target_scope)
            || self
                .attempts
                .values()
                .any(|a| a.action_id == request.action_id)
        {
            return GovernedTurnOutcome::RejectedCurrentState;
        }
        let attempt_id = self.next_attempt_id;
        let Some((action_region_ref, action_digest)) =
            self.committed_actions.get(&request.action_id).cloned()
        else {
            return GovernedTurnOutcome::RejectedPrecondition;
        };
        if let Err(outcome) = self.append(
            CoreEntry::AttemptArmed {
                action_id: request.action_id.clone(),
                attempt_id,
                action_region_ref: action_region_ref.clone(),
                action_digest,
                request_digest: request.target_scope.clone(),
            },
            vec![action_region_ref],
            None,
            None,
            self.projection_digest.clone(),
        ) {
            return outcome;
        }
        self.next_attempt_id += 1;
        self.attempts.insert(
            attempt_id,
            Attempt {
                action_id: request.action_id,
                target_scope: request.target_scope,
                status: AttemptStatus::ArmedUnknown,
                dispatch_identity: None,
                delivered: false,
                ambiguous_delivery: false,
                reserved_delivery: false,
                settlement: None,
            },
        );
        GovernedTurnOutcome::AttemptArmed { attempt_id }
    }
    pub fn record_dispatch_attempt(
        &mut self,
        request: RecordDispatchAttemptRequest,
    ) -> GovernedTurnOutcome {
        let Some(attempt) = self.attempts.get(&request.attempt_id) else {
            return GovernedTurnOutcome::RejectedCurrentState;
        };
        if attempt.status != AttemptStatus::ArmedUnknown || request.dispatch_identity.is_empty() {
            return GovernedTurnOutcome::RejectedCurrentState;
        }
        let action_id = attempt.action_id.clone();
        if let Err(outcome) = self.append(
            CoreEntry::DispatchAttempt {
                action_id,
                attempt_id: request.attempt_id,
                adapter_id: request.dispatch_identity.clone(),
            },
            vec![],
            None,
            None,
            self.projection_digest.clone(),
        ) {
            return outcome;
        }
        let attempt = self
            .attempts
            .get_mut(&request.attempt_id)
            .expect("attempt exists");
        attempt.status = AttemptStatus::Dispatched;
        attempt.dispatch_identity = Some(request.dispatch_identity);
        GovernedTurnOutcome::DispatchRecorded
    }
    pub fn deliver_armed_attempt(
        &mut self,
        request: DeliverArmedAttemptRequest,
    ) -> GovernedTurnOutcome {
        let Some(attempt) = self.attempts.get_mut(&request.attempt_id) else {
            return GovernedTurnOutcome::RejectedCurrentState;
        };
        if attempt.status != AttemptStatus::Dispatched
            || attempt.dispatch_identity.as_deref() != Some(request.dispatch_identity.as_str())
        {
            return GovernedTurnOutcome::RejectedCurrentState;
        }
        if attempt.ambiguous_delivery {
            return GovernedTurnOutcome::Ambiguous;
        }
        if attempt.delivered {
            return GovernedTurnOutcome::DuplicateDelivery;
        }
        if !attempt.reserved_delivery {
            let attempt_id = request.attempt_id;
            if let Err(outcome) = self.append(
                CoreEntry::AdapterReservation { attempt_id },
                vec![],
                None,
                None,
                self.projection_digest.clone(),
            ) {
                return outcome;
            }
            self.attempts
                .get_mut(&attempt_id)
                .expect("attempt exists")
                .reserved_delivery = true;
        }
        self.provider_submissions += 1;
        if let Err(outcome) = self.append(
            CoreEntry::AdapterSubmissionRecorded {
                attempt_id: request.attempt_id,
            },
            vec![],
            None,
            None,
            self.projection_digest.clone(),
        ) {
            self.attempts
                .get_mut(&request.attempt_id)
                .expect("attempt exists")
                .ambiguous_delivery = true;
            return outcome;
        }
        self.attempts
            .get_mut(&request.attempt_id)
            .expect("attempt exists")
            .delivered = true;
        GovernedTurnOutcome::Delivered
    }
    pub fn present_settlement_certificate(
        &mut self,
        request: PresentSettlementCertificateRequest,
    ) -> GovernedTurnOutcome {
        if !self.region_matches(&request.evidence_region_id, &request.evidence_digest) {
            return GovernedTurnOutcome::IntegrityOrProtocolFault;
        }
        let Some(attempt) = self.attempts.get(&request.attempt_id) else {
            return GovernedTurnOutcome::RejectedCurrentState;
        };
        let valid = (request.resolution == "Confirmed"
            && request.proof_class == "ProviderConfirmation")
            || (request.resolution == "NotApplied"
                && request.proof_class == "VerifiableNonExecution");
        if !valid {
            return GovernedTurnOutcome::RejectedInvalidProofClass;
        }
        if attempt.status == AttemptStatus::QuarantinedDispute {
            return GovernedTurnOutcome::RejectedCurrentState;
        }
        if attempt.status == AttemptStatus::Settled {
            if attempt.settlement.as_ref().is_some_and(|settlement| {
                settlement
                    == &(
                        request.resolution.clone(),
                        request.evidence_region_id.clone(),
                        request.evidence_digest.clone(),
                        request.dispatch_identity.clone(),
                    )
            }) {
                return GovernedTurnOutcome::Settled {
                    resolution: request.resolution,
                };
            }
            let action_id = attempt.action_id.clone();
            if let Err(outcome) = self.append(
                CoreEntry::QuarantinedDispute {
                    action_id,
                    attempt_id: request.attempt_id,
                },
                vec![],
                None,
                None,
                self.projection_digest.clone(),
            ) {
                return outcome;
            }
            self.attempts
                .get_mut(&request.attempt_id)
                .expect("attempt exists")
                .status = AttemptStatus::QuarantinedDispute;
            return GovernedTurnOutcome::QuarantinedDispute;
        }
        let pre_dispatch_not_applied = attempt.status == AttemptStatus::ArmedUnknown
            && request.resolution == "NotApplied"
            && request.dispatch_identity.is_empty();
        if !pre_dispatch_not_applied
            && (attempt.status != AttemptStatus::Dispatched
                || attempt.dispatch_identity.as_deref() != Some(request.dispatch_identity.as_str()))
        {
            return GovernedTurnOutcome::RejectedCurrentState;
        }
        let action_id = attempt.action_id.clone();
        if let Err(outcome) = self.append(
            CoreEntry::AttemptSettled {
                action_id,
                attempt_id: request.attempt_id,
                resolution: request.resolution.clone(),
                evidence_region_id: request.evidence_region_id.clone(),
                evidence_digest: request.evidence_digest.clone(),
            },
            vec![request.evidence_region_id.clone()],
            None,
            None,
            self.projection_digest.clone(),
        ) {
            return outcome;
        }
        let attempt = self
            .attempts
            .get_mut(&request.attempt_id)
            .expect("attempt exists");
        attempt.status = AttemptStatus::Settled;
        attempt.settlement = Some((
            request.resolution.clone(),
            request.evidence_region_id.clone(),
            request.evidence_digest.clone(),
            request.dispatch_identity.clone(),
        ));
        GovernedTurnOutcome::Settled {
            resolution: request.resolution,
        }
    }
    pub fn persist_fence(&mut self, generation: u64) -> GovernedTurnOutcome {
        if generation <= self.generation || self.agent_id.is_none() {
            return GovernedTurnOutcome::RejectedPrecondition;
        }
        let (turn, lease, base) = self
            .turn
            .as_ref()
            .filter(|turn| turn.status != TurnStatus::Closed)
            .map_or((None, None, self.projection_digest.clone()), |t| {
                (Some(t.turn_id), t.active_lease, Some(t.base_digest.clone()))
            });
        if let Err(outcome) = self.append(
            CoreEntry::FenceRevoked { generation },
            vec![],
            turn,
            lease,
            base,
        ) {
            return outcome;
        }
        self.generation = generation;
        if let Some(turn) = self.turn.as_mut() {
            turn.active_lease = None;
            turn.status = TurnStatus::Closed;
        }
        GovernedTurnOutcome::GenerationFenced { generation }
    }
    pub fn revoke_capability(&mut self, capability_id: &str) -> GovernedTurnOutcome {
        if capability_id.is_empty() || self.agent_id.is_none() {
            return GovernedTurnOutcome::RejectedPrecondition;
        }
        if let Err(outcome) = self.append(
            CoreEntry::CapabilityRevoked {
                capability_id: capability_id.into(),
            },
            vec![],
            None,
            None,
            self.projection_digest.clone(),
        ) {
            return outcome;
        }
        self.revoked_capabilities.insert(capability_id.into());
        GovernedTurnOutcome::CapabilityRevoked
    }
    pub fn reconstruct_after_crash(&mut self) -> GovernedTurnOutcome {
        let old = self.storage.take();
        drop(old);
        match D1DurableStorage::open(&self.root) {
            Ok(storage) => {
                self.storage = Some(storage);
                self.provider_submissions = 0;
                self.replay();
                GovernedTurnOutcome::Reconstructed
            }
            Err(_) => GovernedTurnOutcome::IntegrityOrProtocolFault,
        }
    }
}
