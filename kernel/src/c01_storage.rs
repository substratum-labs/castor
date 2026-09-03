//! C-01 single-node D1 durable Region and conditional Core journal.
//!
//! The JSON records used here are private implementation state, not a frozen
//! storage or wire schema. A successful outcome is returned only after the
//! corresponding file and containing directory have been synchronized.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum DurabilityProfile {
    D1,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RegionPersisted {
    pub region_ref: String,
    pub content_digest: String,
    pub profile: DurabilityProfile,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EnsureRegionOutcome {
    Success(RegionPersisted),
    AlreadyPersistedSameContent(RegionPersisted),
    RejectedIdentityConflict,
    UnavailableBeforeAck,
    IntegrityFault,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PersistedEntryProof {
    pub agent_id: String,
    pub entry_id: u64,
    pub entry_digest: String,
    pub entry_kind: String,
    pub durability_profile: DurabilityProfile,
    pub expected_projection_digest: String,
    pub referenced_region_digests: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum CoreEntry {
    TurnCommitted {
        turn_id: u64,
        successor_projection_digest: Option<String>,
        action_manifest_digest: Option<String>,
        #[serde(default)]
        action_manifest: Vec<String>,
    },
    LeaseGranted {
        turn_id: u64,
        lease_epoch: u64,
    },
    AttemptArmed {
        action_id: String,
        attempt_id: u64,
        action_region_ref: String,
        action_digest: String,
        request_digest: String,
    },
    DispatchAttempt {
        action_id: String,
        attempt_id: u64,
        adapter_id: String,
    },
    AttemptSettled {
        action_id: String,
        attempt_id: u64,
        resolution: String,
        evidence_region_id: String,
        evidence_digest: String,
    },
    QuarantinedDispute {
        action_id: String,
        attempt_id: u64,
    },
    CapabilityRevoked {
        capability_id: String,
    },
    CapabilityGranted {
        capability_id: String,
        /// Canonical JSON for the grant.  C-01 treats this as opaque durable
        /// payload; C-06 owns its schema and reconstructs the projection.
        grant_json: String,
    },
    AdapterReservation {
        attempt_id: u64,
    },
    AdapterSubmissionRecorded {
        attempt_id: u64,
    },
    FenceRevoked {
        generation: u64,
    },
    InteractionRequested {
        turn_id: u64,
        interaction_id: String,
        request_digest: String,
        service_id: String,
    },
    InteractionBound {
        turn_id: u64,
        interaction_id: String,
        region_id: String,
        result_digest: String,
        disposition: String,
    },
    ConflictingInteractionOutcomeAppended {
        interaction_id: String,
        conflicting_region_id: String,
        conflicting_digest: String,
    },
    InteractionTurnClosed {
        turn_id: u64,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppendConditionalRequest {
    pub agent_id: String,
    pub entry_id: u64,
    pub expected_core_epoch: u64,
    pub expected_agent_generation: Option<u64>,
    pub expected_turn_id: Option<u64>,
    pub expected_lease_epoch: Option<u64>,
    pub expected_base_projection_digest: Option<String>,
    pub entry: CoreEntry,
    pub region_refs: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AppendConditionalOutcome {
    EntryPersisted(PersistedEntryProof),
    AlreadyPersistedSameEntry(PersistedEntryProof),
    RejectedPrecondition {
        current_projection_hint: Option<String>,
    },
    RejectedMissingOrUnpersistedRegion,
    UnavailableBeforeAck,
    IntegrityFault,
}

pub trait DurableStorage {
    fn ensure_region(
        &mut self,
        region_ref: &str,
        content_digest: &str,
        content: &[u8],
        profile: DurabilityProfile,
    ) -> EnsureRegionOutcome;

    fn append_conditional(&mut self, request: AppendConditionalRequest)
        -> AppendConditionalOutcome;

    fn read_entry(&self, agent_id: &str, entry_id: u64) -> Option<PersistedEntryProof>;
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct RegionRecord {
    persisted: RegionPersisted,
    data_file: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct JournalRecord {
    request: AppendConditionalRequest,
    proof: PersistedEntryProof,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct AuthorityState {
    core_epoch: u64,
    agent_generation: Option<u64>,
    turn_id: Option<u64>,
    lease_epoch: Option<u64>,
    projection_digest: Option<String>,
}

impl AuthorityState {
    fn from_request(request: &AppendConditionalRequest) -> Self {
        Self {
            core_epoch: request.expected_core_epoch,
            agent_generation: request.expected_agent_generation,
            turn_id: request.expected_turn_id,
            lease_epoch: request.expected_lease_epoch,
            projection_digest: request.expected_base_projection_digest.clone(),
        }
    }

    fn accepts(&self, request: &AppendConditionalRequest) -> bool {
        self.core_epoch == request.expected_core_epoch
            && self.agent_generation == request.expected_agent_generation
            && self.turn_id == request.expected_turn_id
            && self.lease_epoch == request.expected_lease_epoch
            && self.projection_digest == request.expected_base_projection_digest
    }

    fn apply(&mut self, request: &AppendConditionalRequest, proof: &PersistedEntryProof) {
        match &request.entry {
            CoreEntry::LeaseGranted {
                turn_id,
                lease_epoch,
            } => {
                self.turn_id = Some(*turn_id);
                self.lease_epoch = Some(*lease_epoch);
            }
            CoreEntry::TurnCommitted {
                successor_projection_digest,
                ..
            } => {
                self.turn_id = None;
                self.lease_epoch = None;
                self.projection_digest = successor_projection_digest
                    .clone()
                    .or_else(|| Some(proof.entry_digest.clone()));
            }
            CoreEntry::FenceRevoked { generation } => {
                self.agent_generation = Some(*generation);
                self.turn_id = None;
                self.lease_epoch = None;
            }
            CoreEntry::InteractionRequested { turn_id, .. } => {
                self.turn_id = Some(*turn_id);
                self.lease_epoch = None;
            }
            CoreEntry::InteractionTurnClosed { .. } => {
                self.turn_id = None;
                self.lease_epoch = None;
            }
            CoreEntry::AttemptArmed { .. }
            | CoreEntry::DispatchAttempt { .. }
            | CoreEntry::AttemptSettled { .. }
            | CoreEntry::QuarantinedDispute { .. }
            | CoreEntry::CapabilityGranted { .. }
            | CoreEntry::CapabilityRevoked { .. }
            | CoreEntry::AdapterReservation { .. }
            | CoreEntry::AdapterSubmissionRecorded { .. } => {
                self.projection_digest = Some(proof.entry_digest.clone());
            }
            CoreEntry::InteractionBound { .. }
            | CoreEntry::ConflictingInteractionOutcomeAppended { .. } => {}
        }
    }
}

pub struct D1DurableStorage {
    root: PathBuf,
    _ownership_lock: Option<File>,
    regions: HashMap<String, RegionRecord>,
    entries: HashMap<(String, u64), JournalRecord>,
    authority: HashMap<String, AuthorityState>,
    healthy: bool,
    fail_after_journal_write_once: bool,
}

impl D1DurableStorage {
    pub fn open(root: impl AsRef<Path>) -> io::Result<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        let ownership_lock = acquire_ownership_lock(&root, ".c01-writer.lock")?;
        Self::open_replayed(root, Some(ownership_lock))
    }

    pub(crate) fn open_snapshot(root: impl AsRef<Path>) -> io::Result<Self> {
        Self::open_replayed(root.as_ref().to_path_buf(), None)
    }

    pub fn inspect(root: impl AsRef<Path>) -> io::Result<()> {
        Self::open_snapshot(root).map(|_| ())
    }

    fn open_replayed(root: PathBuf, ownership_lock: Option<File>) -> io::Result<Self> {
        fs::create_dir_all(root.join("regions"))?;
        sync_directory(&root)?;

        let regions = load_regions(&root)?;
        let records = load_journal(&root)?;
        let mut entries = HashMap::new();
        let mut authority = HashMap::new();

        for record in records {
            validate_record(&record, &regions)?;
            let key = (record.request.agent_id.clone(), record.request.entry_id);
            if entries.insert(key, record.clone()).is_some() {
                return Err(invalid_data("duplicate journal entry identity"));
            }
            apply_record_authority(&mut authority, &record)?;
        }

        Ok(Self {
            root,
            _ownership_lock: ownership_lock,
            regions,
            entries,
            authority,
            healthy: true,
            fail_after_journal_write_once: false,
        })
    }

    pub fn read_region(&self, region_ref: &str) -> Option<Vec<u8>> {
        if !self.healthy {
            return None;
        }
        let record = self.regions.get(region_ref)?;
        fs::read(self.root.join("regions").join(&record.data_file)).ok()
    }

    pub fn resolve_entry(&self, proof: &PersistedEntryProof) -> Option<AppendConditionalRequest> {
        if !self.healthy {
            return None;
        }
        let record = self
            .entries
            .get(&(proof.agent_id.clone(), proof.entry_id))?;
        (record.proof == *proof).then(|| record.request.clone())
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn journal_entries(&self) -> usize {
        self.entries.len()
    }

    /// Ordered replay input for the sole Core projection owner.
    pub fn journal_requests(&self) -> Vec<AppendConditionalRequest> {
        let mut requests: Vec<_> = self
            .entries
            .values()
            .map(|record| record.request.clone())
            .collect();
        requests.sort_by(|left, right| {
            left.agent_id
                .cmp(&right.agent_id)
                .then(left.entry_id.cmp(&right.entry_id))
        });
        requests
    }

    pub fn is_healthy(&self) -> bool {
        self.healthy
    }

    pub fn inject_failure_after_next_journal_write(&mut self) {
        self.fail_after_journal_write_once = true;
    }

    fn persist_region(
        &self,
        region_ref: &str,
        content_digest: &str,
        content: &[u8],
        profile: DurabilityProfile,
    ) -> io::Result<RegionRecord> {
        let stem = hex_sha256(region_ref.as_bytes());
        let data_file = format!("{stem}.bin");
        let metadata_file = format!("{stem}.json");
        let region_dir = self.root.join("regions");

        atomic_write(&region_dir.join(&data_file), content)?;
        let record = RegionRecord {
            persisted: RegionPersisted {
                region_ref: region_ref.to_string(),
                content_digest: content_digest.to_string(),
                profile,
            },
            data_file,
        };
        let metadata = serde_json::to_vec(&record).map_err(invalid_json)?;
        atomic_write(&region_dir.join(metadata_file), &metadata)?;
        Ok(record)
    }

    fn append_record(&mut self, record: &JournalRecord) -> io::Result<()> {
        let mut encoded = serde_json::to_vec(record).map_err(invalid_json)?;
        encoded.push(b'\n');
        let mut journal = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.root.join("core-journal.jsonl"))?;
        journal.write_all(&encoded)?;
        if std::mem::take(&mut self.fail_after_journal_write_once) {
            return Err(io::Error::other(
                "injected failure after journal write and before durable acknowledgement",
            ));
        }
        journal.sync_all()?;
        sync_directory(&self.root)
    }
}

impl DurableStorage for D1DurableStorage {
    fn ensure_region(
        &mut self,
        region_ref: &str,
        content_digest: &str,
        content: &[u8],
        profile: DurabilityProfile,
    ) -> EnsureRegionOutcome {
        if !self.healthy {
            return EnsureRegionOutcome::IntegrityFault;
        }
        if region_ref.is_empty() || content_digest != digest_label(content) {
            return EnsureRegionOutcome::RejectedIdentityConflict;
        }

        if let Some(existing) = self.regions.get(region_ref) {
            let existing_bytes = self.read_region(region_ref);
            if existing.persisted.content_digest == content_digest
                && existing.persisted.profile == profile
            {
                return if existing_bytes.as_deref() == Some(content) {
                    EnsureRegionOutcome::AlreadyPersistedSameContent(existing.persisted.clone())
                } else {
                    EnsureRegionOutcome::IntegrityFault
                };
            }
            return EnsureRegionOutcome::RejectedIdentityConflict;
        }

        match self.persist_region(region_ref, content_digest, content, profile) {
            Ok(record) => {
                let persisted = record.persisted.clone();
                self.regions.insert(region_ref.to_string(), record);
                EnsureRegionOutcome::Success(persisted)
            }
            Err(error) if error.kind() == io::ErrorKind::InvalidData => {
                self.healthy = false;
                EnsureRegionOutcome::IntegrityFault
            }
            Err(_) => {
                self.healthy = false;
                EnsureRegionOutcome::UnavailableBeforeAck
            }
        }
    }

    fn append_conditional(
        &mut self,
        request: AppendConditionalRequest,
    ) -> AppendConditionalOutcome {
        if !self.healthy {
            return AppendConditionalOutcome::IntegrityFault;
        }
        let key = (request.agent_id.clone(), request.entry_id);
        let entry_digest = match request_digest(&request) {
            Ok(digest) => digest,
            Err(_) => return AppendConditionalOutcome::IntegrityFault,
        };

        if let Some(existing) = self.entries.get(&key) {
            if existing.proof.entry_digest == entry_digest && existing.request == request {
                return AppendConditionalOutcome::AlreadyPersistedSameEntry(existing.proof.clone());
            }
            return AppendConditionalOutcome::IntegrityFault;
        }

        let mut referenced_region_digests = Vec::with_capacity(request.region_refs.len());
        for region_ref in &request.region_refs {
            let Some(region) = self.regions.get(region_ref) else {
                return AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion;
            };
            referenced_region_digests.push(region.persisted.content_digest.clone());
        }
        if let CoreEntry::AttemptArmed {
            action_region_ref,
            action_digest,
            ..
        } = &request.entry
        {
            let Some(action_region) = self.regions.get(action_region_ref) else {
                return AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion;
            };
            if !request.region_refs.contains(action_region_ref)
                || action_region.persisted.content_digest != *action_digest
            {
                return AppendConditionalOutcome::IntegrityFault;
            }
        }

        if !is_capability_control_entry(&request.entry) {
            if let Some(current) = self.authority.get(&request.agent_id) {
                if !current.accepts(&request) {
                    return AppendConditionalOutcome::RejectedPrecondition {
                        current_projection_hint: current.projection_digest.clone(),
                    };
                }
            }
        }

        let proof = PersistedEntryProof {
            agent_id: request.agent_id.clone(),
            entry_id: request.entry_id,
            entry_digest,
            entry_kind: entry_kind(&request.entry).to_string(),
            durability_profile: DurabilityProfile::D1,
            expected_projection_digest: request
                .expected_base_projection_digest
                .clone()
                .unwrap_or_default(),
            referenced_region_digests,
        };
        let record = JournalRecord {
            request: request.clone(),
            proof: proof.clone(),
        };

        if self.append_record(&record).is_err() {
            self.healthy = false;
            return AppendConditionalOutcome::UnavailableBeforeAck;
        }

        if !is_capability_control_entry(&request.entry) {
            let state = self
                .authority
                .entry(request.agent_id.clone())
                .or_insert_with(|| AuthorityState::from_request(&request));
            state.apply(&request, &proof);
        }
        self.entries.insert(key, record);
        AppendConditionalOutcome::EntryPersisted(proof)
    }

    fn read_entry(&self, agent_id: &str, entry_id: u64) -> Option<PersistedEntryProof> {
        if !self.healthy {
            return None;
        }
        self.entries
            .get(&(agent_id.to_string(), entry_id))
            .map(|record| record.proof.clone())
    }
}

fn load_regions(root: &Path) -> io::Result<HashMap<String, RegionRecord>> {
    let mut regions = HashMap::new();
    for entry in fs::read_dir(root.join("regions"))? {
        let path = entry?.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let record: RegionRecord =
            serde_json::from_slice(&fs::read(&path)?).map_err(invalid_json)?;
        let expected_data_file =
            format!("{}.bin", hex_sha256(record.persisted.region_ref.as_bytes()));
        if record.data_file != expected_data_file {
            return Err(invalid_data("Region data file identity mismatch"));
        }
        let bytes = fs::read(root.join("regions").join(&record.data_file))?;
        if digest_label(&bytes) != record.persisted.content_digest {
            return Err(invalid_data("Region content digest mismatch"));
        }
        if regions
            .insert(record.persisted.region_ref.clone(), record)
            .is_some()
        {
            return Err(invalid_data("duplicate Region identity"));
        }
    }
    Ok(regions)
}

fn load_journal(root: &Path) -> io::Result<Vec<JournalRecord>> {
    let path = root.join("core-journal.jsonl");
    if !path.exists() {
        return Ok(Vec::new());
    }

    let mut records = Vec::new();
    for line in BufReader::new(File::open(path)?).lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        records.push(serde_json::from_str(&line).map_err(invalid_json)?);
    }
    Ok(records)
}

fn validate_record(
    record: &JournalRecord,
    regions: &HashMap<String, RegionRecord>,
) -> io::Result<()> {
    if record.proof.agent_id != record.request.agent_id
        || record.proof.entry_id != record.request.entry_id
        || record.proof.entry_digest != request_digest(&record.request)?
        || record.proof.entry_kind != entry_kind(&record.request.entry)
        || record.proof.durability_profile != DurabilityProfile::D1
        || record.proof.expected_projection_digest
            != record
                .request
                .expected_base_projection_digest
                .clone()
                .unwrap_or_default()
    {
        return Err(invalid_data("journal proof does not resolve to entry"));
    }
    let expected_regions: Option<Vec<_>> = record
        .request
        .region_refs
        .iter()
        .map(|region_ref| {
            regions
                .get(region_ref)
                .map(|region| region.persisted.content_digest.clone())
        })
        .collect();
    if expected_regions.as_ref() != Some(&record.proof.referenced_region_digests) {
        return Err(invalid_data("journal references a missing Region"));
    }
    Ok(())
}

fn apply_record_authority(
    authority: &mut HashMap<String, AuthorityState>,
    record: &JournalRecord,
) -> io::Result<()> {
    if is_capability_control_entry(&record.request.entry) {
        return Ok(());
    }
    let state = authority
        .entry(record.request.agent_id.clone())
        .or_insert_with(|| AuthorityState::from_request(&record.request));
    if !state.accepts(&record.request) {
        return Err(invalid_data("journal contains stale authority transition"));
    }
    state.apply(&record.request, &record.proof);
    Ok(())
}

fn is_capability_control_entry(entry: &CoreEntry) -> bool {
    matches!(
        entry,
        CoreEntry::CapabilityGranted { .. } | CoreEntry::CapabilityRevoked { .. }
    )
}

fn request_digest(request: &AppendConditionalRequest) -> io::Result<String> {
    serde_json::to_vec(request)
        .map(|bytes| digest_label(&bytes))
        .map_err(invalid_json)
}

fn entry_kind(entry: &CoreEntry) -> &'static str {
    match entry {
        CoreEntry::TurnCommitted { .. } => "TurnCommitted",
        CoreEntry::LeaseGranted { .. } => "LeaseGranted",
        CoreEntry::AttemptArmed { .. } => "AttemptArmed",
        CoreEntry::DispatchAttempt { .. } => "DispatchAttempt",
        CoreEntry::AttemptSettled { .. } => "AttemptSettled",
        CoreEntry::QuarantinedDispute { .. } => "QuarantinedDispute",
        CoreEntry::CapabilityGranted { .. } => "CapabilityGranted",
        CoreEntry::CapabilityRevoked { .. } => "CapabilityRevoked",
        CoreEntry::AdapterReservation { .. } => "AdapterReservation",
        CoreEntry::AdapterSubmissionRecorded { .. } => "AdapterSubmissionRecorded",
        CoreEntry::FenceRevoked { .. } => "FenceRevoked",
        CoreEntry::InteractionRequested { .. } => "InteractionRequested",
        CoreEntry::InteractionBound { .. } => "InteractionBound",
        CoreEntry::ConflictingInteractionOutcomeAppended { .. } => {
            "ConflictingInteractionOutcomeAppended"
        }
        CoreEntry::InteractionTurnClosed { .. } => "InteractionTurnClosed",
    }
}

fn digest_label(bytes: &[u8]) -> String {
    format!("sha256:{}", hex_sha256(bytes))
}

fn hex_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "path has no parent"))?;
    fs::create_dir_all(parent)?;
    let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let temp = parent.join(format!(
        ".{}.{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("state"),
        std::process::id(),
        sequence
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        sync_directory(parent)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
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
