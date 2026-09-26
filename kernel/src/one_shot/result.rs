use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct TaskResult {
    pub task_id: String,
    pub status: &'static str,
    pub failure_reason: &'static str,
    pub workspace_snapshot_sha256: String,
    pub committed_turns: Vec<u64>,
    pub settled_actions_count: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub derived_task_image_digest: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub final_patch_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub patch_diff: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub test_passed: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub test_exit_code: Option<i32>,
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
            derived_task_image_digest: None,
            final_patch_sha256: None,
            patch_diff: None,
            test_passed: None,
            test_exit_code: None,
        }
    }

    pub fn image_build_failure(task_id: String, snapshot_hash: String) -> Self {
        Self {
            task_id,
            status: "FAILED",
            failure_reason: "PROVISIONING_IMAGE_BUILD_FAILED",
            workspace_snapshot_sha256: snapshot_hash,
            committed_turns: vec![],
            settled_actions_count: 0,
            derived_task_image_digest: None,
            final_patch_sha256: None,
            patch_diff: None,
            test_passed: None,
            test_exit_code: None,
        }
    }

    pub fn after_image(
        task_id: String,
        snapshot_hash: String,
        image_digest: String,
        reason: &'static str,
        test_exit_code: Option<i32>,
    ) -> Self {
        Self {
            task_id,
            status: "FAILED",
            failure_reason: reason,
            workspace_snapshot_sha256: snapshot_hash,
            committed_turns: vec![],
            settled_actions_count: 0,
            derived_task_image_digest: Some(image_digest),
            final_patch_sha256: None,
            patch_diff: None,
            test_passed: test_exit_code.map(|code| code == 0),
            test_exit_code,
        }
    }
}
