use castor_kernel::c01_storage::D1DurableStorage;
use castor_kernel::c04_adapter::inspect_adapter_store;
use std::env;
use std::io;
use std::path::PathBuf;

fn usage() -> &'static str {
    "usage: castord --state-dir PATH --check"
}

fn parse_args() -> Result<PathBuf, String> {
    let mut args = env::args().skip(1);
    let mut state_dir = None;
    let mut check = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--state-dir" => {
                state_dir = Some(PathBuf::from(
                    args.next()
                        .ok_or_else(|| "--state-dir requires a path".to_string())?,
                ));
            }
            "--check" => check = true,
            _ => return Err(format!("unknown argument: {arg}")),
        }
    }
    if !check {
        return Err("only --check mode is implemented in this vertical slice".to_string());
    }
    state_dir.ok_or_else(|| "--state-dir is required".to_string())
}

fn check(state_dir: PathBuf) -> io::Result<()> {
    let core_root = state_dir.join("core");
    let adapter_root = state_dir.join("adapter");
    if !core_root.join("regions").is_dir() || !adapter_root.join("adapter-config.json").is_file() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "castord state root is not initialized",
        ));
    }
    D1DurableStorage::open(&core_root)?;
    let identity = inspect_adapter_store(&adapter_root, &core_root)?;
    println!(
        "castord state valid: adapter={} assurance_profile={}",
        identity.adapter_id, identity.assurance_profile
    );
    Ok(())
}

fn main() {
    let result = parse_args().map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error));
    if let Err(error) = result.and_then(check) {
        eprintln!("{error}\n{}", usage());
        std::process::exit(2);
    }
}
