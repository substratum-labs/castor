//! Bounded host-owned workspace actuator for the one-shot test runtime.

use crate::host::{GatewayClient, SyscallRequest};
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{self, ErrorKind, Write};
use std::path::{Component, Path};
use std::process::{Command, Stdio};

const EVIDENCE_KEY: &[u8] = b"castor-one-shot-test-evidence-key";
pub const ACTUATOR_ISSUER: &str = "castor-one-shot-workspace-actuator";

pub struct SettledEdit {
    pub patch: String,
    pub patch_sha256: String,
    pub target_path: String,
}

pub fn evidence_key_hex() -> String {
    key_hex(EVIDENCE_KEY)
}

pub fn key_hex(key: &[u8]) -> String {
    hex(key)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn request(socket: &Path, id: &str, op: &str, payload: Value) -> io::Result<Value> {
    let mut client = GatewayClient::connect(socket)?;
    let response = client.request(&SyscallRequest {
        request_id: id.to_owned(),
        op: op.to_owned(),
        payload,
    })?;
    if response.status != "Ok" {
        return Err(io::Error::other(format!(
            "{op} rejected: {:?}",
            response.error
        )));
    }
    response
        .outcome
        .ok_or_else(|| io::Error::other("missing gateway outcome"))
}

fn expect_type(value: Value, expected: &str) -> io::Result<Value> {
    if value.get("type").and_then(Value::as_str) != Some(expected) {
        return Err(io::Error::other(format!("expected {expected}: {value}")));
    }
    Ok(value)
}

fn required_str<'a>(value: &'a Value, field: &str) -> io::Result<&'a str> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, format!("missing {field}")))
}

/// Only a committed and armed action is eligible for host application.
pub fn settle_workspace_edit(
    control_socket: &Path,
    actuator_socket: &Path,
    evidence_socket: &Path,
    workspace: &Path,
    settle: bool,
) -> io::Result<Option<SettledEdit>> {
    settle_workspace_edit_with_key(
        control_socket,
        actuator_socket,
        evidence_socket,
        workspace,
        settle,
        EVIDENCE_KEY,
    )
}

pub fn settle_workspace_edit_with_key(
    control_socket: &Path,
    actuator_socket: &Path,
    evidence_socket: &Path,
    workspace: &Path,
    settle: bool,
    key: &[u8],
) -> io::Result<Option<SettledEdit>> {
    let journal = request(control_socket, "inspect-edit", "InspectJournal", json!({}))?;
    let Some(armed) = journal["entries"]
        .as_array()
        .and_then(|entries| entries.iter().find_map(|entry| entry.get("AttemptArmed")))
    else {
        return Ok(None);
    };
    let attempt_id = armed["attempt_id"]
        .as_u64()
        .ok_or_else(|| io::Error::other("missing attempt ID"))?;
    let action_id = required_str(armed, "action_id")?;
    let scope = required_str(armed, "request_digest")?;
    if action_id != "action-1" || !scope.starts_with("workspace:") {
        return Err(io::Error::new(
            ErrorKind::PermissionDenied,
            "action exceeds bounded test actuator scope",
        ));
    }
    expect_type(
        request(
            control_socket,
            "dispatch-edit",
            "RecordDispatchAttempt",
            json!({
                "attempt_id": attempt_id,
                "dispatch_identity": "edit-1"
            }),
        )?,
        "DispatchRecorded",
    )?;
    let envelope = request(
        actuator_socket,
        "acquire-edit",
        "AcquireDispatch",
        json!({
            "attempt_id": attempt_id,
            "dispatch_identity": "edit-1",
            "actuator_id": "c04:generic"
        }),
    )?;
    if required_str(&envelope, "delivery_outcome")? != "Delivered"
        || required_str(&envelope, "target_scope")? != scope
    {
        return Err(io::Error::other("actuator delivery binding mismatch"));
    }
    let payload: Vec<u8> = serde_json::from_value(envelope["payload"].clone())
        .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
    let digest = format!("sha256:{:x}", Sha256::digest(&payload));
    if required_str(&envelope, "payload_digest")? != digest {
        return Err(io::Error::other("actuator payload digest mismatch"));
    }
    let edit: Value = serde_json::from_slice(&payload)
        .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
    let target_path = required_str(&edit, "target_path")?;
    if edit["action_type"] != "WorkspaceEdit" || scope != format!("workspace:{target_path}") {
        return Err(io::Error::new(
            ErrorKind::PermissionDenied,
            "unsupported test workspace action",
        ));
    }
    let patch = required_str(&edit, "patch")?.to_owned();
    apply_patch(workspace, target_path, &patch)?;
    let patch_sha256 = format!("sha256:{:x}", Sha256::digest(patch.as_bytes()));
    if settle {
        settle_receipt_with_key(control_socket, evidence_socket, attempt_id, scope, key)?;
    }
    Ok(Some(SettledEdit {
        patch,
        patch_sha256,
        target_path: target_path.to_owned(),
    }))
}

pub fn apply_patch(workspace: &Path, target_path: &str, patch: &str) -> io::Result<()> {
    validate_patch(workspace, target_path, patch)?;
    let mut child = Command::new("git")
        .args(["apply", "--whitespace=nowarn", "-"])
        .current_dir(workspace)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()?;
    child
        .stdin
        .take()
        .expect("piped patch input")
        .write_all(patch.as_bytes())?;
    let applied = child.wait_with_output()?;
    if !applied.status.success() {
        return Err(io::Error::other(format!(
            "workspace patch failed: {}",
            String::from_utf8_lossy(&applied.stderr)
        )));
    }
    Ok(())
}

pub fn validate_patch(workspace: &Path, target: &str, patch: &str) -> io::Result<()> {
    let target_path = Path::new(target);
    let forbidden_metadata = [
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
        "new file mode ",
        "deleted file mode ",
        "GIT binary patch",
        "Binary files ",
    ];
    let diff_headers: Vec<_> = patch
        .lines()
        .filter(|line| line.starts_with("diff --git "))
        .collect();
    if !target_path
        .components()
        .all(|part| matches!(part, Component::Normal(_)))
        || !fs::symlink_metadata(workspace.join(target_path))?
            .file_type()
            .is_file()
        || (diff_headers.len() > 1
            || diff_headers
                .first()
                .is_some_and(|header| *header != format!("diff --git a/{target} b/{target}")))
        || patch.lines().any(|line| {
            forbidden_metadata
                .iter()
                .any(|prefix| line.starts_with(prefix))
        })
        || !patch.lines().any(|line| line == format!("--- a/{target}"))
        || !patch.lines().any(|line| line == format!("+++ b/{target}"))
        || patch
            .lines()
            .filter(|line| line.starts_with("--- "))
            .count()
            != 1
        || patch
            .lines()
            .filter(|line| line.starts_with("+++ "))
            .count()
            != 1
    {
        return Err(io::Error::new(
            ErrorKind::PermissionDenied,
            "patch exceeds workspace target",
        ));
    }
    let mut child = Command::new("git")
        .args(["apply", "--check", "-"])
        .current_dir(workspace)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    child
        .stdin
        .take()
        .expect("piped patch check")
        .write_all(patch.as_bytes())?;
    if !child.wait()?.success() {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "patch cannot apply cleanly",
        ));
    }
    Ok(())
}

pub fn settle_receipt(
    control_socket: &Path,
    evidence_socket: &Path,
    attempt_id: u64,
    scope: &str,
) -> io::Result<()> {
    settle_receipt_with_key(
        control_socket,
        evidence_socket,
        attempt_id,
        scope,
        EVIDENCE_KEY,
    )
}

fn settle_receipt_with_key(
    control_socket: &Path,
    evidence_socket: &Path,
    attempt_id: u64,
    scope: &str,
    key: &[u8],
) -> io::Result<()> {
    let mut receipt = json!({
        "attempt_id": attempt_id,
        "stable_operation_id": "edit-1",
        "request_digest": scope,
        "issuer": ACTUATOR_ISSUER,
        "adapter_id": "c04:generic",
        "settlement_schema_version": 1,
        "resolution": "Confirmed",
        "actuator_state": "Committed"
    });
    let bytes = serde_json::to_vec(&receipt).map_err(io::Error::other)?;
    let mut mac = Hmac::<Sha256>::new_from_slice(key).map_err(io::Error::other)?;
    mac.update(&bytes);
    receipt["signature"] = json!(hex(&mac.finalize().into_bytes()));
    let evidence_bytes = serde_json::to_vec(&receipt).map_err(io::Error::other)?;
    let evidence_digest = format!("sha256:{:x}", Sha256::digest(&evidence_bytes));
    expect_type(
        request(
            control_socket,
            "persist-edit-evidence",
            "EnsureRegion",
            json!({
                "region_ref": "region://settlement-receipt",
                "content_digest": evidence_digest,
                "content": evidence_bytes,
                "profile": "D1"
            }),
        )?,
        "Success",
    )?;
    receipt["dispatch_identity"] = json!("edit-1");
    receipt["evidence_region_id"] = json!("region://settlement-receipt");
    receipt["evidence_digest"] = json!(evidence_digest);
    receipt["proof_class"] = json!("ProviderConfirmation");
    expect_type(
        request(
            evidence_socket,
            "settle-edit",
            "PresentSettlementCertificate",
            receipt,
        )?,
        "Settled",
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{apply_patch, validate_patch};
    use std::fs;

    #[test]
    fn second_rename_section_cannot_escape_single_file_scope() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("defect.txt"), "bad\n").unwrap();
        fs::write(root.path().join("other.txt"), "secret\n").unwrap();
        let patch = "--- a/defect.txt\n+++ b/defect.txt\n@@ -1 +1 @@\n-bad\n+good\n\
diff --git a/other.txt b/renamed.txt\n\
similarity index 100%\n\
rename from other.txt\n\
rename to renamed.txt\n";
        assert!(
            validate_patch(root.path(), "defect.txt", patch).is_err(),
            "a second metadata-only file operation must be rejected"
        );
    }

    #[test]
    fn nested_workspace_edit_changes_only_its_bound_target() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("src")).unwrap();
        fs::write(root.path().join("src/lib.rs"), "fn broken() {}\n").unwrap();
        fs::write(root.path().join("secret.txt"), "secret\n").unwrap();
        let patch =
            "--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@\n-fn broken() {}\n+fn fixed() {}\n";
        apply_patch(root.path(), "src/lib.rs", patch).unwrap();
        assert_eq!(
            fs::read_to_string(root.path().join("src/lib.rs")).unwrap(),
            "fn fixed() {}\n"
        );
        assert_eq!(
            fs::read_to_string(root.path().join("secret.txt")).unwrap(),
            "secret\n"
        );
    }
}
