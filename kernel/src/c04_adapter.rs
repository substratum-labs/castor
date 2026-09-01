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

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DispatchCommand {
    pub agent_id: String,
    pub action_id: String,
    pub attempt_id: u64,
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
}

#[derive(Clone, Debug, Serialize, Deserialize)]
enum AdapterEvent {
    Reserved(AdapterDedupRecord),
    SubmissionAttempted(AdapterDedupRecord),
    Observation(EffectObservationReport),
}

pub struct D1EffectAdapter<P: EffectProvider> {
    root: PathBuf,
    core_root: PathBuf,
    config: AdapterConfig,
    provider: P,
    dedup: HashMap<DedupKey, AdapterDedupRecord>,
    observations: HashMap<ObservationKey, EffectObservationReport>,
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
        let core_root = fs::canonicalize(core_root.as_ref())?;
        let root = fs::canonicalize(root)?;
        let config = AdapterConfig {
            adapter_id: adapter_id.to_string(),
            assurance_profile: assurance_profile.to_string(),
            core_root: core_root.to_string_lossy().into_owned(),
        };
        let bytes = serde_json::to_vec(&config).map_err(invalid_json)?;
        atomic_write(&root.join("adapter-config.json"), &bytes)?;
        Ok(Self {
            root,
            core_root,
            config,
            provider,
            dedup: HashMap::new(),
            observations: HashMap::new(),
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
        let (mut dedup, observations) = replay_events(&root, &config)?;
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
            core_root,
            config,
            provider,
            dedup,
            observations,
        })
    }

    pub fn adapter_id(&self) -> &str {
        &self.config.adapter_id
    }

    pub fn assurance_profile(&self) -> &str {
        &self.config.assurance_profile
    }

    fn append_event(&self, event: &AdapterEvent) -> io::Result<()> {
        let mut bytes = serde_json::to_vec(event).map_err(invalid_json)?;
        bytes.push(b'\n');
        let mut journal = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.root.join("adapter-journal.jsonl"))?;
        journal.write_all(&bytes)?;
        journal.sync_all()?;
        sync_directory(&self.root)
    }

    fn validate_command(&self, command: &DispatchCommand) -> Result<DedupKey, String> {
        for (name, value) in [
            ("agent_id", command.agent_id.as_str()),
            ("action_id", command.action_id.as_str()),
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

        let storage = D1DurableStorage::open(&self.core_root)
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
                ref request_digest,
            } if action_id == &command.action_id
                && attempt_id == command.attempt_id
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
        if let Some(existing) = self.observations.get(&key) {
            return existing == &report;
        }
        if self
            .append_event(&AdapterEvent::Observation(report.clone()))
            .is_err()
        {
            return false;
        }
        self.observations.insert(key, report);
        true
    }
}

pub fn authority_binding_digest(command: &DispatchCommand) -> String {
    let binding = (
        &command.agent_id,
        &command.action_id,
        command.attempt_id,
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

fn replay_events(
    root: &Path,
    config: &AdapterConfig,
) -> io::Result<(
    HashMap<DedupKey, AdapterDedupRecord>,
    HashMap<ObservationKey, EffectObservationReport>,
)> {
    let path = root.join("adapter-journal.jsonl");
    if !path.exists() {
        return Ok((HashMap::new(), HashMap::new()));
    }
    let mut dedup = HashMap::new();
    let mut observations = HashMap::new();
    for line in BufReader::new(File::open(path)?).lines() {
        let line = line?;
        if line.is_empty() {
            return Err(invalid_data("empty adapter journal record"));
        }
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
                if let Some(existing) = observations.get(&key) {
                    if existing != &report {
                        return Err(invalid_data("conflicting observation identity"));
                    }
                } else {
                    observations.insert(key, report);
                }
            }
        }
    }
    Ok((dedup, observations))
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

fn invalid_json(error: serde_json::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
