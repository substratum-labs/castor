use castor_kernel::one_shot::manifest::TaskManifest;
use castor_kernel::one_shot::verifier::IsolatedVerifier;
use serde_json::json;
use std::path::Path;

fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    assert_eq!(arguments.len(), 3, "candidate workspace and fresh evidence dir required");
    let workspace = Path::new(&arguments[1]);
    let state = Path::new(&arguments[2]);
    assert!(!state.exists(), "audit evidence directory must be fresh");
    let manifest: TaskManifest = serde_json::from_value(json!({
        "task_id": "task-t372-independent-candidate-audit",
        "idempotency_key": "task-t372-independent-candidate-audit",
        "carrier_base_image": "unused",
        "workspace_snapshot_path": "unused.tar",
        "workspace_snapshot_sha256": "unused",
        "task_prompt": "unused",
        "verification_command": ["sh", "tests/t372_verify_original_mathutils.sh"],
        "verification_timeout_seconds": 60
    })).unwrap();
    let result = IsolatedVerifier::run(&manifest, workspace, state, false);
    let evidence = json!({"workspace": workspace, "code": result.code,
        "reason": result.reason, "evidence": result.evidence});
    std::fs::create_dir_all(state).unwrap();
    std::fs::write(state.join("candidate-result.json"), serde_json::to_vec_pretty(&evidence).unwrap()).unwrap();
    println!("{}", evidence);
    std::process::exit(if result.code == 0 { 0 } else { 1 });
}
