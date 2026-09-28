use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct ExcludedEntry {
    pub path: String,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArchivedEntry {
    pub path: String,
    pub bytes: usize,
    pub sha256: String,
    pub mode: u32,
    pub origin: String,
}

#[derive(Debug, Serialize)]
pub struct PackReceipt {
    pub schema_version: u32,
    pub source_commit: String,
    pub task_id: String,
    pub idempotency_key: String,
    pub carrier_base_image: String,
    pub verifier_image_pin: String,
    pub workspace_snapshot_sha256: String,
    pub excluded_entries: Vec<ExcludedEntry>,
    pub archived_entries: Vec<ArchivedEntry>,
}
