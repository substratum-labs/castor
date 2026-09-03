//! Single-store D1 governed-turn composition.

use crate::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
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
    CapabilityGranted,
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

/// The closed D1 authorization vocabulary. New rights require an RFC change.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CapabilityRight {
    AdmitTurn,
    RegisterAction,
    Revoke,
    Derive,
}

/// Finite, Core-decidable constraints accepted in D1 grants.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Constraint {
    ExactMatch { key: String, value: String },
    ScopePrefix { prefix: String },
    NumericUpperBound { metric: String, limit: u64 },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilityGrant {
    pub cap_id: String,
    pub subject: String,
    pub object_ref: String,
    pub rights: Vec<CapabilityRight>,
    pub constraints: Vec<Constraint>,
    pub parent_cap_id: Option<String>,
    pub revocation_domain: Option<String>,
    pub delegation_allowed: bool,
    pub max_turns: Option<u64>,
}

/// Privileged control-plane input; this is deliberately not an AISA request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GrantCapabilityRequest {
    pub grant: CapabilityGrant,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeriveCapabilityRequest {
    pub parent_cap_id: String,
    pub child_subject: String,
    pub child_rights: Vec<CapabilityRight>,
    pub child_object_ref: String,
    pub child_constraints: Vec<Constraint>,
    pub child_delegation_allowed: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CapabilityDeriveOutcome {
    Derived { cap_id: String },
    Rejected(GovernedTurnOutcome),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdmitTurnRequest {
    pub agent_id: String,
    pub turn_id: u64,
    pub lease_epoch: u64,
    pub base_projection_digest: String,
    pub cap_id: Option<String>,
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
    pub cap_id: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActionRegistrationRequest {
    pub action_id: String,
    pub agent_id: String,
    pub action_family: String,
    pub cap_id: String,
    pub target_scope: String,
    pub numeric_parameters: BTreeMap<String, u64>,
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
    capabilities: HashMap<String, CapabilityGrant>,
    capability_generations: HashMap<String, u64>,
    action_capabilities: HashMap<String, String>,
    attempts: HashMap<u64, Attempt>,
    revoked_capabilities: HashSet<String>,
    next_entry_id: u64,
    next_attempt_id: u64,
    provider_submissions: u64,
}

impl D1GovernedTurnAuthority {
    /// Records a privileged grant in the single durable journal and projection.
    pub fn grant_capability(&mut self, request: GrantCapabilityRequest) -> GovernedTurnOutcome {
        let grant = request.grant;
        if grant.cap_id.is_empty()
            || grant.subject.is_empty()
            || grant.object_ref.is_empty()
            || grant.rights.is_empty()
        {
            return GovernedTurnOutcome::RejectedPrecondition;
        }
        if let Some(existing) = self.capabilities.get(&grant.cap_id) {
            return if existing == &grant {
                GovernedTurnOutcome::CapabilityGranted
            } else {
                GovernedTurnOutcome::RejectedPrecondition
            };
        }
        if self.agent_id.is_none() {
            self.agent_id = Some(grant.subject.clone());
        }
        let grant_json = match serde_json::to_string(&grant) {
            Ok(json) => json,
            Err(_) => return GovernedTurnOutcome::IntegrityOrProtocolFault,
        };
        if let Err(outcome) = self.append(
            CoreEntry::CapabilityGranted {
                capability_id: grant.cap_id.clone(),
                grant_json,
            },
            vec![],
            None,
            None,
            self.projection_digest.clone(),
        ) {
            return outcome;
        }
        let capability_id = grant.cap_id.clone();
        self.capabilities.insert(capability_id.clone(), grant);
        self.capability_generations
            .insert(capability_id, self.generation);
        GovernedTurnOutcome::CapabilityGranted
    }

    pub fn derive_capability(
        &mut self,
        request: DeriveCapabilityRequest,
    ) -> CapabilityDeriveOutcome {
        let parent = match self.active_capability(&request.parent_cap_id) {
            Ok(parent) => parent.clone(),
            Err(outcome) => return CapabilityDeriveOutcome::Rejected(outcome),
        };
        if !parent.delegation_allowed
            || !parent.rights.contains(&CapabilityRight::Derive)
            || !rights_are_subset(&request.child_rights, &parent.rights)
            || request.child_object_ref != parent.object_ref
            || !constraints_attenuate(&request.child_constraints, &parent.constraints)
        {
            return CapabilityDeriveOutcome::Rejected(GovernedTurnOutcome::RejectedPrecondition);
        }
        let child = CapabilityGrant {
            cap_id: derived_capability_id(&request),
            subject: request.child_subject,
            object_ref: request.child_object_ref,
            rights: request.child_rights,
            constraints: request.child_constraints,
            parent_cap_id: Some(parent.cap_id),
            revocation_domain: parent.revocation_domain,
            delegation_allowed: request.child_delegation_allowed,
            max_turns: parent.max_turns,
        };
        let cap_id = child.cap_id.clone();
        match self.grant_capability(GrantCapabilityRequest { grant: child }) {
            GovernedTurnOutcome::CapabilityGranted => CapabilityDeriveOutcome::Derived { cap_id },
            outcome => CapabilityDeriveOutcome::Rejected(outcome),
        }
    }

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
            capabilities: HashMap::new(),
            capability_generations: HashMap::new(),
            action_capabilities: HashMap::new(),
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

    /// Models loss of the host adapter's non-durable dedup projection.  A
    /// dispatch record remains durable, but the host must fail closed rather
    /// than risk repeating provider I/O.
    pub fn lose_adapter_dedup_state(&mut self) {
        for attempt in self.attempts.values_mut() {
            if attempt.status == AttemptStatus::Dispatched && !attempt.delivered {
                attempt.ambiguous_delivery = true;
            }
        }
    }

    /// Persists a Region through the authority's exclusively owned C-01 store.
    ///
    /// This is the host gateway mechanism entry point; it does not open a
    /// second storage handle or introduce an additional semantic writer.
    pub fn ensure_region(
        &mut self,
        region_ref: &str,
        content_digest: &str,
        content: &[u8],
        profile: DurabilityProfile,
    ) -> EnsureRegionOutcome {
        self.storage_mut()
            .ensure_region(region_ref, content_digest, content, profile)
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
    fn active_capability(
        &self,
        capability_id: &str,
    ) -> Result<&CapabilityGrant, GovernedTurnOutcome> {
        let mut current = capability_id;
        let mut seen = HashSet::new();
        loop {
            if !seen.insert(current) {
                return Err(GovernedTurnOutcome::RejectedPrecondition);
            }
            if self.revoked_capabilities.contains(current) {
                return Err(GovernedTurnOutcome::RejectedCapabilityRevoked);
            }
            let capability = self
                .capabilities
                .get(current)
                .ok_or(GovernedTurnOutcome::RejectedPrecondition)?;
            match capability.parent_cap_id.as_deref() {
                Some(parent) => current = parent,
                None => {
                    return self
                        .capabilities
                        .get(capability_id)
                        .ok_or(GovernedTurnOutcome::RejectedPrecondition)
                }
            }
        }
    }
    fn validate_capability(
        &self,
        capability_id: &str,
        subject: Option<&str>,
        right: CapabilityRight,
        object_ref: Option<&str>,
    ) -> Result<&CapabilityGrant, GovernedTurnOutcome> {
        let capability = self.active_capability(capability_id)?;
        if !capability.rights.contains(&right)
            || subject.is_some_and(|subject| capability.subject != subject)
            || object_ref.is_some_and(|object| capability.object_ref != object)
        {
            return Err(GovernedTurnOutcome::RejectedPrecondition);
        }
        Ok(capability)
    }
    fn validate_turn_capability(
        &self,
        capability_id: &str,
        agent_id: &str,
    ) -> Result<(), GovernedTurnOutcome> {
        let capability = self
            .capabilities
            .get(capability_id)
            .ok_or(GovernedTurnOutcome::RejectedPrecondition)?;
        if self.revoked_capabilities.contains(capability_id) {
            return Err(GovernedTurnOutcome::RejectedCapabilityRevoked);
        }
        if self.capability_generations.get(capability_id) != Some(&self.generation) {
            return Err(GovernedTurnOutcome::RejectedStaleAuthority);
        }
        if !capability.rights.contains(&CapabilityRight::AdmitTurn)
            || (capability.parent_cap_id.is_none() && capability.subject != agent_id)
        {
            return Err(GovernedTurnOutcome::RejectedPrecondition);
        }
        Ok(())
    }
    fn replay(&mut self) {
        let requests = self.storage().journal_requests();
        self.generation = 1;
        self.agent_id = None;
        self.projection_digest = None;
        self.turn = None;
        self.committed_actions.clear();
        self.registered_actions.clear();
        self.capabilities.clear();
        self.capability_generations.clear();
        self.action_capabilities.clear();
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
                CoreEntry::CapabilityGranted { grant_json, .. } => {
                    if let Ok(grant) = serde_json::from_str::<CapabilityGrant>(&grant_json) {
                        self.capability_generations
                            .insert(grant.cap_id.clone(), self.generation);
                        self.capabilities.insert(grant.cap_id.clone(), grant);
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
        if self.turn.as_ref().is_some_and(|turn| {
            turn.status != TurnStatus::Closed || request.turn_id <= turn.turn_id
        }) || request.lease_epoch != 0
            || request.agent_id.is_empty()
            || self
                .agent_id
                .as_deref()
                .is_some_and(|agent| agent != request.agent_id)
        {
            return GovernedTurnOutcome::RejectedPrecondition;
        }
        if let Some(capability_id) = request.cap_id.as_deref() {
            if let Err(outcome) = self.validate_turn_capability(capability_id, &request.agent_id) {
                return outcome;
            }
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
        if let Some(capability_id) = request.cap_id.as_deref() {
            if let Err(outcome) = self.validate_turn_capability(
                capability_id,
                self.agent_id.as_deref().unwrap_or_default(),
            ) {
                return outcome;
            }
        }
        if turn.status != TurnStatus::Ready
            || turn.active_lease != Some(request.lease_epoch)
            || turn.base_digest != request.base_projection_digest
            || (request.cap_id.is_none() && turn.interactions.is_empty())
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
        if self.capabilities.contains_key(&request.cap_id) {
            if self.capability_generations.get(&request.cap_id) != Some(&self.generation) {
                return GovernedTurnOutcome::RejectedStaleAuthority;
            }
            let capability = match self.validate_capability(
                &request.cap_id,
                Some(&request.agent_id),
                CapabilityRight::RegisterAction,
                Some(&request.action_family),
            ) {
                Ok(capability) => capability,
                Err(outcome) => return outcome,
            };
            if !constraints_allow(
                capability,
                &request.target_scope,
                &request.numeric_parameters,
            ) {
                return GovernedTurnOutcome::RejectedPrecondition;
            }
            self.action_capabilities
                .insert(request.action_id.clone(), request.cap_id.clone());
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
        if let Some(bound_capability_id) = self.action_capabilities.get(&request.action_id) {
            if bound_capability_id != &request.capability_id {
                return GovernedTurnOutcome::RejectedPrecondition;
            }
            if self.capability_generations.get(bound_capability_id) != Some(&self.generation) {
                return GovernedTurnOutcome::RejectedStaleAuthority;
            }
            if let Err(outcome) = self.active_capability(bound_capability_id) {
                return outcome;
            }
        } else if self.revoked_capabilities.contains(&request.capability_id) {
            // Compatibility for Phase 1 registrations, which did not retain a
            // grant binding but did carry a revocation identifier at arming.
            return GovernedTurnOutcome::RejectedCapabilityRevoked;
        }
        if self.scope_locked(&request.target_scope)
            || self
                .attempts
                .values()
                .any(|a| a.action_id == request.action_id && a.status != AttemptStatus::Settled)
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

fn rights_are_subset(child: &[CapabilityRight], parent: &[CapabilityRight]) -> bool {
    !child.is_empty() && child.iter().all(|right| parent.contains(right))
}

fn constraints_attenuate(child: &[Constraint], parent: &[Constraint]) -> bool {
    parent.iter().all(|parent_constraint| match parent_constraint {
        Constraint::ExactMatch { key, value } => child.iter().any(|candidate| {
            matches!(candidate, Constraint::ExactMatch { key: child_key, value: child_value }
                if child_key == key && child_value == value)
        }),
        Constraint::NumericUpperBound { metric, limit } => child.iter().any(|candidate| {
            matches!(candidate, Constraint::NumericUpperBound { metric: child_metric, limit: child_limit }
                if child_metric == metric && child_limit <= limit)
        }),
        Constraint::ScopePrefix { prefix } => child.iter().any(|candidate| {
            matches!(candidate, Constraint::ScopePrefix { prefix: child_prefix }
                if scope_is_within(child_prefix, prefix))
        }),
    })
}

fn scope_is_within(scope: &str, prefix: &str) -> bool {
    scope == prefix
        || scope
            .strip_prefix(prefix)
            .is_some_and(|suffix| suffix.starts_with('/'))
}

fn constraints_allow(
    capability: &CapabilityGrant,
    target_scope: &str,
    numeric_parameters: &BTreeMap<String, u64>,
) -> bool {
    !target_scope.split('/').any(|part| part == "..")
        && capability
            .constraints
            .iter()
            .all(|constraint| match constraint {
                Constraint::ExactMatch { .. } => true,
                Constraint::ScopePrefix { prefix } => scope_is_within(target_scope, prefix),
                Constraint::NumericUpperBound { metric, limit } => numeric_parameters
                    .get(metric)
                    .is_some_and(|value| value <= limit),
            })
}

fn derived_capability_id(request: &DeriveCapabilityRequest) -> String {
    let canonical = serde_json::to_vec(&(
        &request.parent_cap_id,
        &request.child_subject,
        &request.child_rights,
        &request.child_object_ref,
        &request.child_constraints,
        request.child_delegation_allowed,
    ))
    .expect("capability derivation request is serializable");
    format!("cap:{:x}", Sha256::digest(canonical))
}
