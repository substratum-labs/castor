use std::process::Command;

fn dynamic_dependencies(binary: &str) -> String {
    #[cfg(target_os = "macos")]
    let command = "otool";
    #[cfg(target_os = "linux")]
    let command = "ldd";
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    compile_error!("zero-Python runtime audit requires macOS or Linux dynamic-link inspection");

    let mut inspector = Command::new(command);
    #[cfg(target_os = "macos")]
    inspector.arg("-L");
    let output = inspector
        .arg(binary)
        .output()
        .expect("inspect binary dependencies");
    assert!(
        output.status.success(),
        "dynamic dependency inspection failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).to_lowercase()
}

#[test]
fn castor_task_cli_has_no_python_runtime_link() {
    let dependencies = dynamic_dependencies(env!("CARGO_BIN_EXE_castor"));
    assert!(
        !dependencies.contains("libpython") && !dependencies.contains("python.framework"),
        "Rust castor CLI must run without Python: {dependencies}"
    );
}

#[test]
fn castord_authority_has_no_python_runtime_link() {
    let dependencies = dynamic_dependencies(env!("CARGO_BIN_EXE_castord"));
    assert!(
        !dependencies.contains("libpython") && !dependencies.contains("python.framework"),
        "Rust castord must run without Python: {dependencies}"
    );
}
