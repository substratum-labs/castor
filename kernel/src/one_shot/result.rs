use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct TaskResult {
    pub task_id: String,
    pub status: &'static str,
    pub failure_reason: &'static str,
    pub workspace_snapshot_sha256: String,
    pub committed_turns: Vec<u64>,
    pub settled_actions_count: u64,
}

impl TaskResult {
    pub fn snapshot_failure(task_id: String, expected_hash: String) -> Self {
        Self {
            task_id,
            status: "FAILED",
            failure_reason: "SNAPSHOT_DIGEST_MISMATCH",
            workspace_snapshot_sha256: expected_hash,
            committed_turns: vec![],
            settled_actions_count: 0,
        }
    }
}
