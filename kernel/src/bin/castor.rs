#[path = "castor/developer_controller.rs"]
mod developer_controller;
#[path = "castor/developer_entry.rs"]
mod developer_entry;
use castor_kernel::one_shot::image::{valid_digest, StagedSnapshot};
use castor_kernel::one_shot::install::InstalledRelease;
use castor_kernel::one_shot::manifest::TaskManifest;
use castor_kernel::one_shot::project::engine::{
    pack_project, pack_project_with_carrier, run_project, run_project_with_pins,
};
use castor_kernel::one_shot::result::TaskResult;
use castor_kernel::one_shot::runtime_prepare::{
    check_node_major, prepare, DockerEngine, EngineOps,
};
use castor_kernel::one_shot::supervisor::{
    run_product_task, run_test_task, test_state_root, RunOutcome,
};
use std::env;
use std::io;
use std::path::PathBuf;
use std::process::ExitCode;

fn main() -> ExitCode {
    match run() {
        Ok(code) => code,
        Err(error) => {
            eprintln!("castor: {error}");
            ExitCode::from(2)
        }
    }
}

fn run() -> io::Result<ExitCode> {
    let mut args = env::args().skip(1);
    let command = args.next().ok_or_else(invalid_args)?;
    if command == "__controller" {
        return developer_controller::run_controller();
    }
    if command == "__engine-helper" {
        return developer_controller::engine_helper(&args.next().ok_or_else(invalid_args)?);
    }
    if command == "runtime" {
        if args.next().as_deref() != Some("prepare") || args.next().is_some() {
            return Err(invalid_args());
        }
        let release = InstalledRelease::load_current()?;
        check_node_major()?;
        let home = env::var_os("HOME").ok_or_else(|| io::Error::other("missing HOME"))?;
        let receipt = prepare(
            &release,
            &DockerEngine,
            &PathBuf::from(home).join(".castor"),
        )?;
        println!(
            "{}",
            serde_json::to_string(&receipt).map_err(io::Error::other)?
        );
        return Ok(ExitCode::SUCCESS);
    }
    if command != "run" && command != "pack" {
        return Err(invalid_args());
    }
    let mut manifest_path = None;
    let mut project_path = None;
    let mut spec_path = None;
    let mut out_path = None;
    let mut allow_test_opcodes = false;
    let mut model_mode = None;
    let mut state_root = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--task" if manifest_path.is_none() => manifest_path = args.next().map(PathBuf::from),
            "--project" if project_path.is_none() => project_path = args.next().map(PathBuf::from),
            "--task-spec" if spec_path.is_none() => spec_path = args.next().map(PathBuf::from),
            "--out" if out_path.is_none() => out_path = args.next().map(PathBuf::from),
            "--allow-test-opcodes" => allow_test_opcodes = true,
            "--model" if model_mode.is_none() => model_mode = args.next(),
            "--state-root" if state_root.is_none() => state_root = args.next().map(PathBuf::from),
            _ => return Err(invalid_args()),
        }
    }
    if command == "pack" {
        if allow_test_opcodes
            || manifest_path.is_some()
            || model_mode.is_some()
            || state_root.is_some()
        {
            return Err(invalid_args());
        }
        let project = project_path.ok_or_else(invalid_args)?;
        let spec = spec_path.ok_or_else(invalid_args)?;
        let out = out_path.ok_or_else(invalid_args)?;
        let executable = std::fs::canonicalize(env::current_exe()?)?;
        let installed_layout = executable.file_name().is_some_and(|name| name == "castor")
            && executable
                .parent()
                .and_then(|parent| parent.file_name())
                .is_some_and(|name| name == "bin");
        let receipt = if installed_layout {
            let release = InstalledRelease::load_current()?;
            let engine = DockerEngine;
            let platform = engine.platform()?;
            if platform.os != "linux" {
                return Err(io::Error::other("Castor requires a Linux Docker Engine"));
            }
            let pins = release.pins(&platform.arch)?;
            let carrier = format!("{}@{}", pins.carrier_tag, pins.carrier.image_id);
            pack_project_with_carrier(&project, &spec, &out, &carrier)?
        } else {
            pack_project(&project, &spec, &out)?
        };
        println!(
            "{}",
            serde_json::to_string(&receipt).map_err(io::Error::other)?
        );
        return Ok(ExitCode::SUCCESS);
    }
    if out_path.is_some()
        || (state_root.is_some() && model_mode.is_none())
        || model_mode
            .as_deref()
            .is_some_and(|mode| mode != "local-ollama")
        || (model_mode.is_some()
            && (project_path.is_none() || manifest_path.is_some() || allow_test_opcodes))
        || (project_path.is_some() != spec_path.is_some())
        || (project_path.is_some() && manifest_path.is_some())
    {
        return Err(invalid_args());
    }
    if let (Some(project), Some(spec)) = (project_path, spec_path) {
        if allow_test_opcodes {
            return Err(invalid_args());
        }
        if model_mode.is_some() {
            if !project.is_dir() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "project must be a directory",
                ));
            }
            if !spec.is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "task spec must be a file",
                ));
            }
            return developer_entry::run(&project, &spec, state_root.as_deref());
        }
        manifest_path = Some(
            match (
                env::var("CASTOR_CONTROLLER_CARRIER_REF").ok(),
                env::var("CASTOR_CONTROLLER_VERIFIER_ID").ok(),
            ) {
                (Some(carrier), Some(verifier)) => {
                    run_project_with_pins(&project, &spec, &carrier, &verifier)?
                }
                (None, None) => run_project(&project, &spec)?,
                _ => return Err(io::Error::other("incomplete controller release pins")),
            },
        );
    }
    let manifest_path = manifest_path.ok_or_else(invalid_args)?;
    let manifest = TaskManifest::read(&manifest_path)?;
    match manifest.validate_snapshot(&manifest_path) {
        Ok(snapshot) => {
            let staged = match StagedSnapshot::stage(snapshot, &manifest.workspace_snapshot_path) {
                Ok(staged) => staged,
                Err(error) => {
                    eprintln!("castor: image staging failed: {error}");
                    return image_failure(manifest);
                }
            };
            let test_child = if allow_test_opcodes {
                env::var_os("CASTOR_TEST_AGENT_CHILD").map(PathBuf::from)
            } else {
                None
            };
            let image_digest = if test_child.is_some() {
                let digest = env::var("CASTOR_TEST_DERIVED_IMAGE_DIGEST").map_err(|_| {
                    io::Error::new(io::ErrorKind::InvalidInput, "missing test image digest")
                })?;
                if !valid_digest(&digest) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "invalid test image digest",
                    ));
                }
                digest
            } else {
                match staged.build(&manifest.carrier_base_image) {
                    Ok(digest) => digest,
                    Err(error) => {
                        eprintln!("castor: image build failed: {error}");
                        return image_failure(manifest);
                    }
                }
            };
            let outcome = if let Some(child) = test_child {
                run_test_task(
                    &manifest,
                    &manifest_path,
                    &staged,
                    image_digest,
                    &test_state_root()?,
                    &child,
                )?
            } else {
                let state_root = env::var_os("CASTOR_STATE_ROOT")
                    .map(PathBuf::from)
                    .or_else(|| {
                        env::var_os("HOME").map(|home| PathBuf::from(home).join(".castor/state"))
                    })
                    .ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::InvalidInput,
                            "missing HOME or CASTOR_STATE_ROOT",
                        )
                    })?;
                let model_socket = env::var_os("CASTOR_MODEL_SOCKET")
                    .map(PathBuf::from)
                    .unwrap_or_default();
                run_product_task(
                    &manifest,
                    &manifest_path,
                    &staged,
                    image_digest,
                    &state_root,
                    &model_socket,
                )?
            };
            match outcome {
                RunOutcome::Active(value) => {
                    println!(
                        "{}",
                        serde_json::to_string(&value).map_err(io::Error::other)?
                    );
                    Ok(ExitCode::SUCCESS)
                }
                RunOutcome::Replayed(value) => {
                    let success =
                        value.get("status").and_then(|status| status.as_str()) == Some("SUCCEEDED");
                    println!(
                        "{}",
                        serde_json::to_string(&value).map_err(io::Error::other)?
                    );
                    Ok(if success {
                        ExitCode::SUCCESS
                    } else {
                        ExitCode::FAILURE
                    })
                }
                RunOutcome::Terminal(result) => {
                    println!(
                        "{}",
                        serde_json::to_string(&result).map_err(io::Error::other)?
                    );
                    Ok(if result.status == "SUCCEEDED" {
                        ExitCode::SUCCESS
                    } else {
                        ExitCode::FAILURE
                    })
                }
            }
        }
        Err(_) => {
            let result =
                TaskResult::snapshot_failure(manifest.task_id, manifest.workspace_snapshot_sha256);
            println!(
                "{}",
                serde_json::to_string(&result).map_err(io::Error::other)?
            );
            Ok(ExitCode::FAILURE)
        }
    }
}

fn image_failure(manifest: TaskManifest) -> io::Result<ExitCode> {
    let result =
        TaskResult::image_build_failure(manifest.task_id, manifest.workspace_snapshot_sha256);
    println!(
        "{}",
        serde_json::to_string(&result).map_err(io::Error::other)?
    );
    Ok(ExitCode::FAILURE)
}

fn invalid_args() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "usage: castor pack --project PATH --task-spec SPEC_JSON --out DIR | castor run --project PATH --task-spec SPEC_JSON [--model local-ollama [--state-root PATH]] | castor run [--allow-test-opcodes] --task MANIFEST",
    )
}
