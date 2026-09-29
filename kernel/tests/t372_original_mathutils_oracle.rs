//! Opt-in T-372 oracle: original upstream tests in the product IsolatedVerifier.

use castor_kernel::one_shot::manifest::TaskManifest;
use castor_kernel::one_shot::verifier::IsolatedVerifier;
use serde_json::json;
use std::path::Path;

#[test]
#[ignore = "physical oracle: invoke explicitly with T372_ORACLE_BASELINE, T372_ORACLE_REFERENCE and T372_ORACLE_STATE"]
fn original_bits_regression_is_red_then_green_inside_isolated_verifier() {
    let baseline = std::env::var("T372_ORACLE_BASELINE").expect("baseline path");
    let reference = std::env::var("T372_ORACLE_REFERENCE").expect("reference path");
    let state = std::env::var("T372_ORACLE_STATE").expect("durable state path");
    let manifest: TaskManifest = serde_json::from_value(json!({
        "task_id": "task-t372-bits-oracle",
        "idempotency_key": "task-t372-bits-oracle",
        "carrier_base_image": "unused",
        "workspace_snapshot_path": "unused.tar",
        "workspace_snapshot_sha256": "unused",
        "task_prompt": "unused",
        "verification_command": ["sh", "tests/t372_verify_original_mathutils.sh"],
        "verification_timeout_seconds": 60
    }))
    .unwrap();
    let red = IsolatedVerifier::run(
        &manifest,
        Path::new(&baseline),
        Path::new(&state).join("red").as_path(),
        false,
    );
    let green = IsolatedVerifier::run(
        &manifest,
        Path::new(&reference),
        Path::new(&state).join("green").as_path(),
        false,
    );
    std::fs::create_dir_all(&state).unwrap();
    std::fs::write(
        Path::new(&state).join("result.json"),
        serde_json::to_vec_pretty(&json!({
            "baseline": {"code": red.code, "reason": red.reason, "evidence": red.evidence},
            "reference": {"code": green.code, "reason": green.reason, "evidence": green.evidence}
        }))
        .unwrap(),
    )
    .unwrap();
    assert_eq!(red.reason, "TEST_VERIFICATION_FAILED");
    assert_ne!(red.code, 0);
    assert_eq!(green.reason, "NONE");
    assert_eq!(green.code, 0);
    assert!(red.evidence.container_removed && green.evidence.container_removed);
    assert!(red.evidence.inspected_profile.is_some() && green.evidence.inspected_profile.is_some());
}
