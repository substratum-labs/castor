//! C-04 single-node D1 proof-bound effect adapter.
//!
//! The adapter journal is private implementation state. Its durable reservation
//! is a one-way gate: once a key is present, recovery never submits that key to
//! the provider again, even when external application remains unknown.

use crate::c01_storage::{CoreEntry, D1DurableStorage, PersistedEntryProof};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DispatchCommand {
    pub agent_id: String,
    pub action_id: String,
    pub attempt_id: u64,
    pub action_region_ref: String,
    pub action_digest: String,
    pub request_digest: String,
    pub adapter_id: String,
    pub assurance_profile: String,
    pub attempt_armed_proof: PersistedEntryProof,
    pub dispatch_proof: PersistedEntryProof,
    pub authority_binding_digest: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReservationState {
    Reserved,
    SubmissionAttempted,
    Ambiguous,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExternalKnowledge {
    NotApplicable,
    Observed,
    Unknown,
}

type DedupKey = (String, String, u64, String, String);
type ObservationKey = (String, String, u64, String, String);
type ReplayState = (
    HashMap<DedupKey, AdapterDedupRecord>,
    HashMap<ObservationKey, Vec<EffectObservationReport>>,
    AdapterHead,
);

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdapterDedupRecord {
    pub dedup_key: DedupKey,
    pub dispatch_entry_digest: String,
    pub reservation_state: ReservationState,
    pub submission_observation: Option<String>,
    pub external_knowledge: ExternalKnowledge,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EffectObservationReport {
    pub agent_id: String,
    pub action_id: String,
    pub attempt_id: u64,
    pub observation_id: String,
    pub observation_digest: String,
    pub adapter_id: String,
    pub evidence_ref: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DeliverOutcome {
    SubmissionObserved {
        accepted_and_durable: bool,
        external_knowledge: ExternalKnowledge,
    },
    DuplicateDelivery {
        accepted_and_durable: bool,
        prior_external_knowledge: ExternalKnowledge,
    },
    NotSubmittedProven,
    Ambiguous {
        accepted_and_durable: bool,
    },
    RejectedInvalidCommand(String),
    UnavailableBeforeReservation,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProviderOutcome {
    Observed(String),
    Unknown,
}

pub trait EffectProvider {
    fn submit(&mut self, command: &DispatchCommand) -> ProviderOutcome;
}

pub trait EffectAdapter {
    fn deliver_armed_attempt(&mut self, command: DispatchCommand) -> DeliverOutcome;
    fn report_effect_observation(&mut self, report: EffectObservationReport) -> bool;
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct AdapterConfig {
    adapter_id: String,
    assurance_profile: String,
    core_root: String,
    lineage_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
struct CoreLineageAnchor {
    adapter_id: String,
    assurance_profile: String,
    lineage_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
struct CoreReservationAnchor {
    lineage_id: String,
    dedup_key: DedupKey,
    dispatch_entry_digest: String,
}

enum ReservationAnchorStatus {
    Created,
    ExistingSame,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
struct AdapterHead {
    event_count: u64,
    last_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AdapterStoreIdentity {
    pub adapter_id: String,
    pub assurance_profile: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
enum AdapterEvent {
    Reserved(AdapterDedupRecord),
    SubmissionAttempted(AdapterDedupRecord),
    Observation(EffectObservationReport),
}

pub struct D1EffectAdapter<P: EffectProvider> {
    root: PathBuf,
    _ownership_lock: File,
    core_root: PathBuf,
    config: AdapterConfig,
    provider: P,
    dedup: HashMap<DedupKey, AdapterDedupRecord>,
    observations: HashMap<ObservationKey, Vec<EffectObservationReport>>,
    head: AdapterHead,
    healthy: bool,
}

pub fn inspect_adapter_store(
    adapter_root: impl AsRef<Path>,
    core_root: impl AsRef<Path>,
) -> io::Result<AdapterStoreIdentity> {
    let root = fs::canonicalize(adapter_root.as_ref())?;
    let core_root = fs::canonicalize(core_root.as_ref())?;
    let config: AdapterConfig =
        serde_json::from_slice(&fs::read(root.join("adapter-config.json"))?)
            .map_err(invalid_json)?;
    if config.core_root != core_root.to_string_lossy() {
        return Err(invalid_data("adapter Core root identity mismatch"));
    }
    verify_core_lineage(&core_root, &config)?;
    replay_events(&root, &config)?;
    Ok(AdapterStoreIdentity {
        adapter_id: config.adapter_id,
        assurance_profile: config.assurance_profile,
    })
}

impl<P: EffectProvider> D1EffectAdapter<P> {
    pub fn initialize(
        adapter_root: impl AsRef<Path>,
        core_root: impl AsRef<Path>,
        adapter_id: &str,
        assurance_profile: &str,
        provider: P,
    ) -> io::Result<Self> {
        validate_identity(adapter_id, "adapter_id")?;
        validate_identity(assurance_profile, "assurance_profile")?;
        let root = adapter_root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        if fs::read_dir(&root)?.next().is_some() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "adapter store is not fresh",
            ));
        }
        let ownership_lock = acquire_ownership_lock(&root, ".c04-writer.lock")?;
        let core_store = D1DurableStorage::open(core_root.as_ref())?;
        if !core_store.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "adapter must be initialized before Core entries exist",
            ));
        }
        let core_root = fs::canonicalize(core_root.as_ref())?;
        let root = fs::canonicalize(root)?;
        let lineage_id = new_lineage_id(&root, &core_root, adapter_id, assurance_profile)?;
        let config = AdapterConfig {
            adapter_id: adapter_id.to_string(),
            assurance_profile: assurance_profile.to_string(),
            core_root: core_root.to_string_lossy().into_owned(),
            lineage_id: lineage_id.clone(),
        };
        let lineage_anchor = CoreLineageAnchor {
            adapter_id: adapter_id.to_string(),
            assurance_profile: assurance_profile.to_string(),
            lineage_id,
        };
        prepare_core_anchor_dirs(&core_root, &lineage_anchor.lineage_id)?;
        persist_immutable(
            &core_lineage_path(&core_root, adapter_id, assurance_profile),
            &serde_json::to_vec(&lineage_anchor).map_err(invalid_json)?,
        )?;
        let bytes = serde_json::to_vec(&config).map_err(invalid_json)?;
        atomic_write(&root.join("adapter-config.json"), &bytes)?;
        atomic_write(&root.join("adapter-journal.jsonl"), b"")?;
        let head = AdapterHead {
            event_count: 0,
            last_digest: digest_bytes(&bytes),
        };
        atomic_write(
            &root.join("adapter-head.json"),
            &serde_json::to_vec(&head).map_err(invalid_json)?,
        )?;
        Ok(Self {
            root,
            _ownership_lock: ownership_lock,
            core_root,
            config,
            provider,
            dedup: HashMap::new(),
            observations: HashMap::new(),
            head,
            healthy: true,
        })
    }

    pub fn open(
        adapter_root: impl AsRef<Path>,
        core_root: impl AsRef<Path>,
        adapter_id: &str,
        assurance_profile: &str,
        provider: P,
    ) -> io::Result<Self> {
        let root = fs::canonicalize(adapter_root.as_ref())?;
        let ownership_lock = acquire_ownership_lock(&root, ".c04-writer.lock")?;
        let core_root = fs::canonicalize(core_root.as_ref())?;
        let config: AdapterConfig =
            serde_json::from_slice(&fs::read(root.join("adapter-config.json"))?)
                .map_err(invalid_json)?;
        if config.adapter_id != adapter_id
            || config.assurance_profile != assurance_profile
            || config.core_root != core_root.to_string_lossy()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "adapter store identity mismatch",
            ));
        }
        verify_core_lineage(&core_root, &config)?;
        let (mut dedup, observations, head) = replay_events(&root, &config)?;
        // A durable reservation without a durable post-submit observation is
        // conservatively ambiguous after process loss. It is never a retry grant.
        for record in dedup.values_mut() {
            if record.reservation_state == ReservationState::Reserved {
                record.reservation_state = ReservationState::Ambiguous;
                record.external_knowledge = ExternalKnowledge::Unknown;
            }
        }
        Ok(Self {
            root,
            _ownership_lock: ownership_lock,
            core_root,
            config,
            provider,
            dedup,
            observations,
            head,
            healthy: true,
        })
    }

    pub fn adapter_id(&self) -> &str {
        &self.config.adapter_id
    }

    pub fn assurance_profile(&self) -> &str {
        &self.config.assurance_profile
    }

    pub fn observation_reports(
        &self,
        agent_id: &str,
        action_id: &str,
        attempt_id: u64,
        adapter_id: &str,
        observation_id: &str,
    ) -> Vec<EffectObservationReport> {
        self.observations
            .get(&(
                agent_id.to_string(),
                action_id.to_string(),
                attempt_id,
                adapter_id.to_string(),
                observation_id.to_string(),
            ))
            .cloned()
            .unwrap_or_default()
    }

    fn append_event(&mut self, event: &AdapterEvent) -> io::Result<()> {
        if !self.healthy {
            return Err(invalid_data("adapter store is unhealthy"));
        }
        match replay_events(&self.root, &self.config) {
            Ok((_, _, disk_head)) if disk_head == self.head => {}
            _ => {
                self.healthy = false;
                return Err(invalid_data("adapter journal continuity lost"));
            }
        }
        let mut bytes = serde_json::to_vec(event).map_err(invalid_json)?;
        bytes.push(b'\n');
        let result = (|| {
            let mut journal = OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.root.join("adapter-journal.jsonl"))?;
            journal.write_all(&bytes)?;
            journal.sync_all()?;
            let next_head = AdapterHead {
                event_count: self.head.event_count + 1,
                last_digest: chained_digest(&self.head.last_digest, &bytes),
            };
            atomic_write(
                &self.root.join("adapter-head.json"),
                &serde_json::to_vec(&next_head).map_err(invalid_json)?,
            )?;
            self.head = next_head;
            Ok(())
        })();
        if result.is_err() {
            self.healthy = false;
        }
        result
    }

    fn reserve_core_anchor(
        &self,
        key: &DedupKey,
        dispatch_entry_digest: &str,
    ) -> io::Result<ReservationAnchorStatus> {
        let anchor = CoreReservationAnchor {
            lineage_id: self.config.lineage_id.clone(),
            dedup_key: key.clone(),
            dispatch_entry_digest: dispatch_entry_digest.to_string(),
        };
        let path = core_reservation_path(&self.core_root, &self.config.lineage_id, key);
        let bytes = serde_json::to_vec(&anchor).map_err(invalid_json)?;
        match persist_immutable(&path, &bytes) {
            Ok(()) => Ok(ReservationAnchorStatus::Created),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let existing: CoreReservationAnchor =
                    serde_json::from_slice(&fs::read(path)?).map_err(invalid_json)?;
                if existing == anchor {
                    Ok(ReservationAnchorStatus::ExistingSame)
                } else {
                    Err(invalid_data("conflicting Core reservation anchor"))
                }
            }
            Err(error) => Err(error),
        }
    }

    fn validate_command(&self, command: &DispatchCommand) -> Result<DedupKey, String> {
        for (name, value) in [
            ("agent_id", command.agent_id.as_str()),
            ("action_id", command.action_id.as_str()),
            ("action_region_ref", command.action_region_ref.as_str()),
            ("action_digest", command.action_digest.as_str()),
            ("request_digest", command.request_digest.as_str()),
        ] {
            if value.is_empty() {
                return Err(format!("{name} must not be empty"));
            }
        }
        if command.adapter_id != self.config.adapter_id
            || command.assurance_profile != self.config.assurance_profile
        {
            return Err("adapter identity or assurance profile mismatch".to_string());
        }
        if command.authority_binding_digest != authority_binding_digest(command) {
            return Err("authority binding digest mismatch".to_string());
        }

        let storage = D1DurableStorage::open_snapshot(&self.core_root)
            .map_err(|error| format!("cannot open Core proof store: {error}"))?;
        let armed = storage
            .resolve_entry(&command.attempt_armed_proof)
            .ok_or_else(|| "AttemptArmed proof is not persisted".to_string())?;
        let dispatch = storage
            .resolve_entry(&command.dispatch_proof)
            .ok_or_else(|| "DispatchAttempt proof is not persisted".to_string())?;

        if armed.agent_id != command.agent_id
            || armed.expected_core_epoch != dispatch.expected_core_epoch
            || armed.expected_agent_generation != dispatch.expected_agent_generation
            || command.attempt_armed_proof.agent_id != command.agent_id
            || command.dispatch_proof.agent_id != command.agent_id
        {
            return Err("Core authority identity mismatch".to_string());
        }
        match armed.entry {
            CoreEntry::AttemptArmed {
                ref action_id,
                attempt_id,
                ref action_region_ref,
                ref action_digest,
                ref request_digest,
            } if action_id == &command.action_id
                && attempt_id == command.attempt_id
                && action_region_ref == &command.action_region_ref
                && action_digest == &command.action_digest
                && request_digest == &command.request_digest => {}
            _ => return Err("AttemptArmed entry mismatch".to_string()),
        }
        match dispatch.entry {
            CoreEntry::DispatchAttempt {
                ref action_id,
                attempt_id,
                ref adapter_id,
            } if action_id == &command.action_id
                && attempt_id == command.attempt_id
                && adapter_id == &command.adapter_id => {}
            _ => return Err("DispatchAttempt entry mismatch".to_string()),
        }
        if dispatch.expected_base_projection_digest.as_deref()
            != Some(command.attempt_armed_proof.entry_digest.as_str())
        {
            return Err("DispatchAttempt does not follow AttemptArmed".to_string());
        }
        if !command
            .attempt_armed_proof
            .referenced_region_digests
            .contains(&command.action_digest)
        {
            return Err("action digest is not bound by AttemptArmed".to_string());
        }
        Ok(dedup_key(command))
    }
}

impl<P: EffectProvider> EffectAdapter for D1EffectAdapter<P> {
    fn deliver_armed_attempt(&mut self, command: DispatchCommand) -> DeliverOutcome {
        let key = match self.validate_command(&command) {
            Ok(key) => key,
            Err(error) => return DeliverOutcome::RejectedInvalidCommand(error),
        };
        if let Some(existing) = self.dedup.get(&key) {
            if existing.dispatch_entry_digest != command.dispatch_proof.entry_digest {
                return DeliverOutcome::RejectedInvalidCommand(
                    "dedup identity reused with a different dispatch entry".to_string(),
                );
            }
            return DeliverOutcome::DuplicateDelivery {
                accepted_and_durable: true,
                prior_external_knowledge: existing.external_knowledge,
            };
        }

        match self.reserve_core_anchor(&key, &command.dispatch_proof.entry_digest) {
            Ok(ReservationAnchorStatus::Created) => {}
            Ok(ReservationAnchorStatus::ExistingSame) => {
                return DeliverOutcome::Ambiguous {
                    accepted_and_durable: true,
                };
            }
            Err(error) if error.kind() == io::ErrorKind::InvalidData => {
                return DeliverOutcome::RejectedInvalidCommand(error.to_string());
            }
            Err(_) => return DeliverOutcome::UnavailableBeforeReservation,
        }

        let reserved = AdapterDedupRecord {
            dedup_key: key.clone(),
            dispatch_entry_digest: command.dispatch_proof.entry_digest.clone(),
            reservation_state: ReservationState::Reserved,
            submission_observation: None,
            external_knowledge: ExternalKnowledge::NotApplicable,
        };
        if self
            .append_event(&AdapterEvent::Reserved(reserved.clone()))
            .is_err()
        {
            return DeliverOutcome::UnavailableBeforeReservation;
        }
        self.dedup.insert(key.clone(), reserved);

        let (knowledge, observation) = match self.provider.submit(&command) {
            ProviderOutcome::Observed(value) => (ExternalKnowledge::Observed, Some(value)),
            ProviderOutcome::Unknown => (ExternalKnowledge::Unknown, None),
        };
        let attempted = AdapterDedupRecord {
            dedup_key: key.clone(),
            dispatch_entry_digest: command.dispatch_proof.entry_digest,
            reservation_state: ReservationState::SubmissionAttempted,
            submission_observation: observation,
            external_knowledge: knowledge,
        };
        if self
            .append_event(&AdapterEvent::SubmissionAttempted(attempted.clone()))
            .is_err()
        {
            if let Some(record) = self.dedup.get_mut(&key) {
                record.reservation_state = ReservationState::Ambiguous;
                record.external_knowledge = ExternalKnowledge::Unknown;
            }
            return DeliverOutcome::Ambiguous {
                accepted_and_durable: true,
            };
        }
        self.dedup.insert(key, attempted);
        DeliverOutcome::SubmissionObserved {
            accepted_and_durable: true,
            external_knowledge: knowledge,
        }
    }

    fn report_effect_observation(&mut self, report: EffectObservationReport) -> bool {
        if report.agent_id.is_empty()
            || report.action_id.is_empty()
            || report.observation_id.is_empty()
            || report.observation_digest.is_empty()
            || report.adapter_id != self.config.adapter_id
        {
            return false;
        }
        let dedup = (
            report.agent_id.clone(),
            report.action_id.clone(),
            report.attempt_id,
            report.adapter_id.clone(),
            self.config.assurance_profile.clone(),
        );
        if !self.dedup.contains_key(&dedup) {
            return false;
        }
        let key = (
            report.agent_id.clone(),
            report.action_id.clone(),
            report.attempt_id,
            report.adapter_id.clone(),
            report.observation_id.clone(),
        );
        if self
            .observations
            .get(&key)
            .is_some_and(|reports| reports.contains(&report))
        {
            return true;
        }
        if self
            .append_event(&AdapterEvent::Observation(report.clone()))
            .is_err()
        {
            return false;
        }
        self.observations.entry(key).or_default().push(report);
        true
    }
}

pub fn authority_binding_digest(command: &DispatchCommand) -> String {
    let binding = (
        &command.agent_id,
        &command.action_id,
        command.attempt_id,
        &command.action_region_ref,
        &command.action_digest,
        &command.request_digest,
        &command.adapter_id,
        &command.assurance_profile,
        &command.attempt_armed_proof,
        &command.dispatch_proof,
    );
    let bytes = serde_json::to_vec(&binding).expect("serializing a dispatch binding cannot fail");
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn dedup_key(command: &DispatchCommand) -> DedupKey {
    (
        command.agent_id.clone(),
        command.action_id.clone(),
        command.attempt_id,
        command.adapter_id.clone(),
        command.assurance_profile.clone(),
    )
}

fn replay_events(root: &Path, config: &AdapterConfig) -> io::Result<ReplayState> {
    let path = root.join("adapter-journal.jsonl");
    let stored_head: AdapterHead =
        serde_json::from_slice(&fs::read(root.join("adapter-head.json"))?).map_err(invalid_json)?;
    let config_bytes = serde_json::to_vec(config).map_err(invalid_json)?;
    let mut computed_head = AdapterHead {
        event_count: 0,
        last_digest: digest_bytes(&config_bytes),
    };
    let mut dedup = HashMap::new();
    let mut observations = HashMap::new();
    for line in BufReader::new(File::open(path)?).lines() {
        let line = line?;
        if line.is_empty() {
            return Err(invalid_data("empty adapter journal record"));
        }
        let mut encoded = line.as_bytes().to_vec();
        encoded.push(b'\n');
        computed_head.event_count += 1;
        computed_head.last_digest = chained_digest(&computed_head.last_digest, &encoded);
        let event: AdapterEvent = serde_json::from_str(&line).map_err(invalid_json)?;
        match event {
            AdapterEvent::Reserved(record) => {
                validate_record_identity(&record, config)?;
                if record.reservation_state != ReservationState::Reserved
                    || record.external_knowledge != ExternalKnowledge::NotApplicable
                    || dedup.insert(record.dedup_key.clone(), record).is_some()
                {
                    return Err(invalid_data("invalid or duplicate reservation"));
                }
            }
            AdapterEvent::SubmissionAttempted(record) => {
                validate_record_identity(&record, config)?;
                let previous = dedup
                    .get(&record.dedup_key)
                    .ok_or_else(|| invalid_data("submission without reservation"))?;
                if previous.dispatch_entry_digest != record.dispatch_entry_digest
                    || previous.reservation_state != ReservationState::Reserved
                    || record.reservation_state != ReservationState::SubmissionAttempted
                    || record.external_knowledge == ExternalKnowledge::NotApplicable
                {
                    return Err(invalid_data("invalid submission transition"));
                }
                dedup.insert(record.dedup_key.clone(), record);
            }
            AdapterEvent::Observation(report) => {
                if report.adapter_id != config.adapter_id {
                    return Err(invalid_data("observation adapter identity mismatch"));
                }
                let dedup_key = (
                    report.agent_id.clone(),
                    report.action_id.clone(),
                    report.attempt_id,
                    report.adapter_id.clone(),
                    config.assurance_profile.clone(),
                );
                if !dedup.contains_key(&dedup_key) {
                    return Err(invalid_data("observation without reservation"));
                }
                let key = (
                    report.agent_id.clone(),
                    report.action_id.clone(),
                    report.attempt_id,
                    report.adapter_id.clone(),
                    report.observation_id.clone(),
                );
                let reports = observations.entry(key).or_insert_with(Vec::new);
                if !reports.contains(&report) {
                    reports.push(report);
                }
            }
        }
    }
    if computed_head != stored_head {
        return Err(invalid_data("adapter journal continuity mismatch"));
    }
    Ok((dedup, observations, stored_head))
}

fn validate_record_identity(record: &AdapterDedupRecord, config: &AdapterConfig) -> io::Result<()> {
    if record.dedup_key.0.is_empty()
        || record.dedup_key.1.is_empty()
        || record.dedup_key.3 != config.adapter_id
        || record.dedup_key.4 != config.assurance_profile
        || record.dispatch_entry_digest.is_empty()
    {
        return Err(invalid_data("adapter record identity mismatch"));
    }
    Ok(())
}

fn validate_identity(value: &str, name: &str) -> io::Result<()> {
    if value.is_empty() {
        Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{name} must not be empty"),
        ))
    } else {
        Ok(())
    }
}

fn new_lineage_id(
    adapter_root: &Path,
    core_root: &Path,
    adapter_id: &str,
    assurance_profile: &str,
) -> io::Result<String> {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| invalid_data("system clock precedes Unix epoch"))?
        .as_nanos();
    let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let material = format!(
        "{}\0{}\0{}\0{}\0{}\0{}\0{}",
        adapter_root.display(),
        core_root.display(),
        adapter_id,
        assurance_profile,
        std::process::id(),
        timestamp,
        sequence
    );
    Ok(digest_bytes(material.as_bytes()))
}

fn core_anchor_dir(core_root: &Path) -> PathBuf {
    core_root.join("c04-anchors")
}

fn core_lineage_path(core_root: &Path, adapter_id: &str, assurance_profile: &str) -> PathBuf {
    let identity = digest_bytes(format!("{adapter_id}\0{assurance_profile}").as_bytes());
    core_anchor_dir(core_root).join(format!("lineage-{}.json", &identity[7..]))
}

fn core_reservation_path(core_root: &Path, lineage_id: &str, key: &DedupKey) -> PathBuf {
    let encoded = serde_json::to_vec(key).expect("dedup key serialization cannot fail");
    let identity = digest_bytes(&encoded);
    core_anchor_dir(core_root)
        .join(format!("reservations-{}", &lineage_id[7..]))
        .join(format!("{}.json", &identity[7..]))
}

fn prepare_core_anchor_dirs(core_root: &Path, lineage_id: &str) -> io::Result<()> {
    let anchor_dir = core_anchor_dir(core_root);
    fs::create_dir_all(&anchor_dir)?;
    sync_directory(core_root)?;
    let reservations = anchor_dir.join(format!("reservations-{}", &lineage_id[7..]));
    fs::create_dir_all(&reservations)?;
    sync_directory(&anchor_dir)?;
    sync_directory(&reservations)
}

fn verify_core_lineage(core_root: &Path, config: &AdapterConfig) -> io::Result<()> {
    let expected = CoreLineageAnchor {
        adapter_id: config.adapter_id.clone(),
        assurance_profile: config.assurance_profile.clone(),
        lineage_id: config.lineage_id.clone(),
    };
    let actual: CoreLineageAnchor = serde_json::from_slice(&fs::read(core_lineage_path(
        core_root,
        &config.adapter_id,
        &config.assurance_profile,
    ))?)
    .map_err(invalid_json)?;
    if actual != expected {
        return Err(invalid_data("adapter Core lineage mismatch"));
    }
    Ok(())
}

fn persist_immutable(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "path has no parent"))?;
    fs::create_dir_all(parent)?;
    let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    sync_directory(parent)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "path has no parent"))?;
    fs::create_dir_all(parent)?;
    let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let tmp = parent.join(format!(
        ".{}.{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("data"),
        std::process::id(),
        sequence
    ));
    let result = (|| {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&tmp, path)?;
        sync_directory(parent)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

fn sync_directory(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

fn acquire_ownership_lock(root: &Path, name: &str) -> io::Result<File> {
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(root.join(name))?;
    lock.try_lock()?;
    sync_directory(root)?;
    Ok(lock)
}

fn invalid_json(error: serde_json::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn digest_bytes(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn chained_digest(previous: &str, event: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(previous.as_bytes());
    hasher.update(event);
    format!("sha256:{:x}", hasher.finalize())
}
