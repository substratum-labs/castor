use castor_kernel::one_shot::manifest::TaskManifest;
use castor_kernel::one_shot::result::TaskResult;
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
    if args.next().as_deref() != Some("run") {
        return Err(invalid_args());
    }
    let mut manifest_path = None;
    let mut allow_test_opcodes = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--task" => manifest_path = args.next().map(PathBuf::from),
            "--allow-test-opcodes" => allow_test_opcodes = true,
            _ => return Err(invalid_args()),
        }
    }
    let manifest_path = manifest_path.ok_or_else(invalid_args)?;
    let manifest = TaskManifest::read(&manifest_path)?;
    match manifest.validate_snapshot(&manifest_path) {
        Ok(_snapshot) => Err(io::Error::new(
            io::ErrorKind::Unsupported,
            if allow_test_opcodes {
                "test task supervision is not implemented yet"
            } else {
                "task image build and supervision are not implemented yet"
            },
        )),
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

fn invalid_args() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "usage: castor run [--allow-test-opcodes] --task MANIFEST",
    )
}
