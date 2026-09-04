//! C-01 single-node D1 durable Region and conditional Core journal.
//!
//! The JSON records used here are private implementation state, not a frozen
//! storage or wire schema. A successful outcome is returned only after the
//! corresponding file and containing directory have been synchronized.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
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
    /// Journal-authoritative pointer to a D-04 materialized projection cache.
    ///
    /// This is deliberately an index fact, rather than a projection
    /// transition: consumers must not let it alter authority state.  Snapshot
    /// materialization and selection are introduced by EPIC-29 Phase 3.
    SnapshotIndex {
        snapshot_id: String,
        last_entry_id: u64,
        snapshot_digest: String,
    },
    TurnCommitted {
        turn_id: u64,
        successor_projection_digest: Option<String>,
        action_manifest_digest: Option<String>,
        #[serde(default)]
        action_manifest: Vec<String>,
        #[serde(default)]
        cap_id: Option<String>,
    },
    ActionRegistered {
        action_id: String,
        cap_id: String,
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

    /// A restart advances the Core epoch without rewriting old journal
    /// records.  The only transition allowed to bridge that boundary is a
    /// fresh lease for the still-open turn; a commit under the old lease is
    /// deliberately not accepted.
    fn accepts_recovery_lease(&self, request: &AppendConditionalRequest) -> bool {
        matches!(request.entry, CoreEntry::LeaseGranted { .. })
            && request.expected_core_epoch == self.core_epoch + 1
            && self.agent_generation == request.expected_agent_generation
            && self.turn_id == request.expected_turn_id
            && (request.expected_lease_epoch.is_none()
                || self.lease_epoch == request.expected_lease_epoch)
            && self.projection_digest == request.expected_base_projection_digest
    }

    fn apply(&mut self, request: &AppendConditionalRequest, proof: &PersistedEntryProof) {
        match &request.entry {
            CoreEntry::SnapshotIndex { .. } => {}
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
            | CoreEntry::ActionRegistered { .. }
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
        self.entries
            .values()
            .filter(|record| !matches!(record.request.entry, CoreEntry::SnapshotIndex { .. }))
            .count()
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

    /// Persists a D-04 projection cache before its journal index is appended.
    /// Until an index refers to this exact digest it is deliberately ignored
    /// during recovery.
    pub(crate) fn persist_snapshot(&self, snapshot_id: &str, bytes: &[u8]) -> io::Result<String> {
        if snapshot_id.is_empty()
            || snapshot_id.contains('/')
            || snapshot_id.contains('\\')
            || snapshot_id.ends_with(".tmp")
        {
            return Err(invalid_data("invalid snapshot identity"));
        }
        atomic_write(
            &self
                .root
                .join("snapshots")
                .join(format!("{snapshot_id}.json")),
            bytes,
        )?;
        Ok(digest_label(bytes))
    }

    /// Returns only the most recent journal-indexed and digest-valid blob.
    /// Tempfiles and every unpointed file are intentionally invisible here.
    pub fn latest_snapshot(&self) -> Option<(u64, Vec<u8>)> {
        let mut indexes: Vec<_> = self
            .entries
            .values()
            .filter_map(|record| match &record.request.entry {
                CoreEntry::SnapshotIndex {
                    snapshot_id,
                    last_entry_id,
                    snapshot_digest,
                } => Some((
                    record.request.entry_id,
                    snapshot_id.as_str(),
                    *last_entry_id,
                    snapshot_digest.as_str(),
                )),
                _ => None,
            })
            .collect();
        indexes.sort_by_key(|(entry_id, ..)| *entry_id);
        let (_, snapshot_id, last_entry_id, expected_digest) = indexes.pop()?;
        let path = self
            .root
            .join("snapshots")
            .join(format!("{snapshot_id}.json"));
        let bytes = match fs::read(path) {
            Ok(bytes) if digest_label(&bytes) == expected_digest => bytes,
            Ok(_) => {
                eprintln!("warning: D-04 snapshot digest mismatch; replaying from genesis");
                return None;
            }
            Err(_) => {
                eprintln!("warning: D-04 snapshot missing; replaying from genesis");
                return None;
            }
        };
        Some((last_entry_id, bytes))
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
        let payload = serde_json::to_vec(record).map_err(invalid_json)?;
        let payload_len = u32::try_from(payload.len())
            .map_err(|_| invalid_data("journal payload exceeds u32 frame length"))?;
        let crc = crc32fast::hash(&payload);
        let mut journal = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.root.join("core-journal.log"))?;
        journal.write_all(&payload_len.to_le_bytes())?;
        journal.write_all(&payload)?;
        journal.write_all(&crc.to_le_bytes())?;
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

        if is_authority_checked_entry(&request.entry) {
            if let Some(current) = self.authority.get(&request.agent_id) {
                if !current.accepts(&request) && !current.accepts_recovery_lease(&request) {
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

        if is_authority_checked_entry(&request.entry) {
            let state = self
                .authority
                .entry(request.agent_id.clone())
                .or_insert_with(|| AuthorityState::from_request(&request));
            if state.accepts_recovery_lease(&request) {
                state.core_epoch = request.expected_core_epoch;
            }
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
    let path = root.join("core-journal.log");
    if !path.exists() {
        if root.join("core-journal.jsonl").exists() {
            return Err(invalid_data("legacy JSONL Core journal requires migration"));
        }
        return Ok(Vec::new());
    }

    let bytes = fs::read(&path)?;
    let mut records = Vec::new();
    let mut offset = 0usize;
    while offset < bytes.len() {
        let valid_offset = offset;
        let remaining = bytes.len() - offset;
        if remaining < 4 {
            truncate_incomplete_journal(&path, root, valid_offset)?;
            break;
        }
        let payload_len = u32::from_le_bytes(bytes[offset..offset + 4].try_into().expect("slice"));
        let payload_len = usize::try_from(payload_len).expect("u32 fits usize");
        offset += 4;
        let Some(frame_len) = payload_len.checked_add(4) else {
            return Err(invalid_data("journal frame length overflow"));
        };
        if bytes.len() - offset < frame_len {
            truncate_incomplete_journal(&path, root, valid_offset)?;
            break;
        }
        let payload_end = offset + payload_len;
        let payload = &bytes[offset..payload_end];
        let stored_crc = u32::from_le_bytes(
            bytes[payload_end..payload_end + 4]
                .try_into()
                .expect("slice"),
        );
        if crc32fast::hash(payload) != stored_crc {
            return Err(invalid_data("corrupted frame CRC"));
        }
        records.push(serde_json::from_slice(payload).map_err(invalid_json)?);
        offset = payload_end + 4;
    }
    Ok(records)
}

fn truncate_incomplete_journal(path: &Path, root: &Path, valid_offset: usize) -> io::Result<()> {
    let journal = OpenOptions::new().write(true).open(path)?;
    journal.set_len(u64::try_from(valid_offset).expect("usize fits u64"))?;
    journal.sync_all()?;
    sync_directory(root)
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
    if !is_authority_checked_entry(&record.request.entry) {
        return Ok(());
    }
    let state = authority
        .entry(record.request.agent_id.clone())
        .or_insert_with(|| AuthorityState::from_request(&record.request));
    if !state.accepts(&record.request) && !state.accepts_recovery_lease(&record.request) {
        return Err(invalid_data("journal contains stale authority transition"));
    }
    if state.accepts_recovery_lease(&record.request) {
        state.core_epoch = record.request.expected_core_epoch;
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

fn is_authority_checked_entry(entry: &CoreEntry) -> bool {
    !is_capability_control_entry(entry) && !matches!(entry, CoreEntry::SnapshotIndex { .. })
}

fn request_digest(request: &AppendConditionalRequest) -> io::Result<String> {
    serde_json::to_vec(request)
        .map(|bytes| digest_label(&bytes))
        .map_err(invalid_json)
}

fn entry_kind(entry: &CoreEntry) -> &'static str {
    match entry {
        CoreEntry::SnapshotIndex { .. } => "SnapshotIndex",
        CoreEntry::TurnCommitted { .. } => "TurnCommitted",
        CoreEntry::LeaseGranted { .. } => "LeaseGranted",
        CoreEntry::AttemptArmed { .. } => "AttemptArmed",
        CoreEntry::ActionRegistered { .. } => "ActionRegistered",
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
