use castor_kernel::c01_storage::{D1DurableStorage, DurabilityProfile, EnsureRegionOutcome};
use castor_kernel::c04_adapter::inspect_adapter_store;
use castor_kernel::c06_composition::*;
use castor_kernel::host::{
    read_framed, write_framed, GatewayError, SyscallRequest, SyscallResponse,
};
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::{self, ErrorKind};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

fn usage() -> &'static str {
    "usage: castord --storage-root PATH --socket PATH [--child CMD]"
}

struct Config {
    storage_root: PathBuf,
    socket: PathBuf,
    child: Option<String>,
}

fn parse_args() -> Result<Config, String> {
    let mut args = env::args().skip(1);
    let mut storage_root = None;
    let mut socket = None;
    let mut child = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--storage-root" => {
                storage_root =
                    Some(PathBuf::from(args.next().ok_or_else(|| {
                        "--storage-root requires a path".to_string()
                    })?));
            }
            "--socket" => {
                socket = Some(PathBuf::from(
                    args.next()
                        .ok_or_else(|| "--socket requires a path".to_string())?,
                ))
            }
            "--child" => {
                child = Some(
                    args.next()
                        .ok_or_else(|| "--child requires a command".to_string())?,
                )
            }
            _ => return Err(format!("unknown argument: {arg}")),
        }
    }
    Ok(Config {
        storage_root: storage_root.ok_or_else(|| "--storage-root is required".to_string())?,
        socket: socket.ok_or_else(|| "--socket is required".to_string())?,
        child,
    })
}

fn bind_socket(socket: &Path) -> io::Result<UnixListener> {
    let parent = socket
        .parent()
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidInput, "socket has no parent"))?;
    fs::create_dir_all(parent)?;
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    if socket.exists() {
        match UnixStream::connect(socket) {
            Ok(_) => {
                return Err(io::Error::new(
                    ErrorKind::AddrInUse,
                    "live castord already owns socket",
                ))
            }
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::ConnectionRefused | ErrorKind::NotFound
                ) =>
            {
                fs::remove_file(socket)?
            }
            Err(error) => return Err(error),
        }
    }
    let listener = UnixListener::bind(socket)?;
    fs::set_permissions(socket, fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

fn malformed(request_id: String, message: impl Into<String>) -> SyscallResponse {
    SyscallResponse {
        request_id,
        status: "Error".into(),
        outcome: None,
        error: Some(GatewayError {
            code: "MalformedRequest".into(),
            message: message.into(),
        }),
    }
}

fn string(payload: &Value, name: &str) -> Result<String, String> {
    payload
        .get(name)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| format!("{name} must be a string"))
}
fn number(payload: &Value, name: &str) -> Result<u64, String> {
    payload
        .get(name)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{name} must be an unsigned integer"))
}
fn outcome_value(outcome: GovernedTurnOutcome) -> Value {
    match outcome {
        GovernedTurnOutcome::AttemptArmed { attempt_id } => {
            json!({"type":"AttemptArmed","attempt_id":attempt_id})
        }
        GovernedTurnOutcome::GenerationFenced { generation } => {
            json!({"type":"GenerationFenced","generation":generation})
        }
        GovernedTurnOutcome::Settled { resolution } => {
            json!({"type":"Settled","resolution":resolution})
        }
        GovernedTurnOutcome::RejectedStaleGeneration { current_generation } => {
            json!({"type":"RejectedStaleGeneration","current_generation":current_generation})
        }
        other => json!({"type": match other {
            GovernedTurnOutcome::Admitted => "Admitted", GovernedTurnOutcome::InteractionRequested => "InteractionRequested", GovernedTurnOutcome::InteractionBound => "InteractionBound", GovernedTurnOutcome::InteractionConsumed => "InteractionConsumed", GovernedTurnOutcome::TurnCommitted => "TurnCommitted", GovernedTurnOutcome::ActionRegistered => "ActionRegistered", GovernedTurnOutcome::DispatchRecorded => "DispatchRecorded", GovernedTurnOutcome::Delivered => "Delivered", GovernedTurnOutcome::DuplicateDelivery => "DuplicateDelivery", GovernedTurnOutcome::QuarantinedDispute => "QuarantinedDispute", GovernedTurnOutcome::CapabilityRevoked => "CapabilityRevoked", GovernedTurnOutcome::Reconstructed => "Reconstructed", GovernedTurnOutcome::Ambiguous => "Ambiguous", GovernedTurnOutcome::RejectedStaleAuthority => "RejectedStaleAuthority", GovernedTurnOutcome::RejectedCapabilityRevoked => "RejectedCapabilityRevoked", GovernedTurnOutcome::RejectedInvalidProofClass => "RejectedInvalidProofClass", GovernedTurnOutcome::RejectedLateOrClosedTurn => "RejectedLateOrClosedTurn", GovernedTurnOutcome::RejectedCurrentState => "RejectedCurrentState", GovernedTurnOutcome::RejectedPrecondition => "RejectedPrecondition", GovernedTurnOutcome::IntegrityOrProtocolFault => "IntegrityOrProtocolFault", GovernedTurnOutcome::UnavailableBeforeAck => "UnavailableBeforeAck", _ => unreachable!() }}),
    }
}
fn ensure_value(outcome: EnsureRegionOutcome) -> Value {
    json!({"type": match outcome { EnsureRegionOutcome::Success(_) => "Success", EnsureRegionOutcome::AlreadyPersistedSameContent(_) => "AlreadyPersistedSameContent", EnsureRegionOutcome::RejectedIdentityConflict => "RejectedIdentityConflict", EnsureRegionOutcome::UnavailableBeforeAck => "UnavailableBeforeAck", EnsureRegionOutcome::IntegrityFault => "IntegrityFault" }})
}

fn dispatch(
    authority: &mut D1GovernedTurnAuthority,
    request: &SyscallRequest,
) -> Result<Value, String> {
    let p = &request.payload;
    let governed = match request.op.as_str() {
        "AdmitTurn" => authority.admit_turn(AdmitTurnRequest {
            agent_id: string(p, "agent_id")?,
            turn_id: number(p, "turn_id")?,
            lease_epoch: number(p, "lease_epoch")?,
            base_projection_digest: string(p, "base_projection_digest")?,
        }),
        "RequestInteraction" => authority.request_interaction(RequestInteractionRequest {
            interaction_id: string(p, "interaction_id")?,
            lease_epoch: number(p, "lease_epoch")?,
            request_digest: string(p, "request_digest")?,
        }),
        "ReportOutcome" | "ReportInteractionOutcome" => {
            authority.report_outcome(InteractionOutcomeReport {
                interaction_id: string(p, "interaction_id")?,
                observation_region_id: string(p, "observation_region_id")?,
                observation_digest: string(p, "observation_digest")?,
            })
        }
        "ConsumeInteraction" => authority.consume_interaction(ConsumeInteractionRequest {
            interaction_id: string(p, "interaction_id")?,
            lease_epoch: number(p, "lease_epoch")?,
        }),
        "CommitTurn" => authority.commit_turn(CommitTurnRequest {
            lease_epoch: number(p, "lease_epoch")?,
            base_projection_digest: string(p, "base_projection_digest")?,
            successor_region_id: string(p, "successor_region_id")?,
            successor_digest: string(p, "successor_digest")?,
            action_manifest_region_id: string(p, "action_manifest_region_id")?,
            action_manifest_digest: string(p, "action_manifest_digest")?,
            action_manifest: p
                .get("action_manifest")
                .and_then(Value::as_array)
                .ok_or_else(|| "action_manifest must be an array".to_string())?
                .iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| "action_manifest entries must be strings".to_string())
                })
                .collect::<Result<_, _>>()?,
        }),
        "RegisterAction" => authority.register_action(ActionRegistrationRequest {
            action_id: string(p, "action_id")?,
        }),
        "PresentAdmissionCertificate" => {
            authority.present_admission_certificate(PresentAdmissionCertificateRequest {
                action_id: string(p, "action_id")?,
                target_scope: string(p, "target_scope")?,
                capability_id: string(p, "capability_id")?,
                generation: number(p, "generation")?,
            })
        }
        "RecordDispatchAttempt" => {
            authority.record_dispatch_attempt(RecordDispatchAttemptRequest {
                attempt_id: number(p, "attempt_id")?,
                dispatch_identity: string(p, "dispatch_identity")?,
            })
        }
        "DeliverArmedAttempt" => authority.deliver_armed_attempt(DeliverArmedAttemptRequest {
            attempt_id: number(p, "attempt_id")?,
            dispatch_identity: string(p, "dispatch_identity")?,
        }),
        "PresentSettlementCertificate" => {
            authority.present_settlement_certificate(PresentSettlementCertificateRequest {
                attempt_id: number(p, "attempt_id")?,
                dispatch_identity: string(p, "dispatch_identity")?,
                evidence_region_id: string(p, "evidence_region_id")?,
                evidence_digest: string(p, "evidence_digest")?,
                proof_class: string(p, "proof_class")?,
                resolution: string(p, "resolution")?,
            })
        }
        "PersistFence" => authority.persist_fence(number(p, "generation")?),
        "RevokeCapability" => authority.revoke_capability(&string(p, "capability_id")?),
        "Replay" => authority.reconstruct_after_crash(),
        "EnsureRegion" => {
            let content = p
                .get("content")
                .and_then(Value::as_array)
                .ok_or_else(|| "content must be an array".to_string())?
                .iter()
                .map(|v| {
                    v.as_u64()
                        .filter(|n| *n <= 255)
                        .map(|n| n as u8)
                        .ok_or_else(|| "content entries must be bytes".to_string())
                })
                .collect::<Result<Vec<_>, _>>()?;
            return Ok(ensure_value(authority.ensure_region(
                &string(p, "region_ref")?,
                &string(p, "content_digest")?,
                &content,
                DurabilityProfile::D1,
            )));
        }
        "__ProviderSubmissionCount" => {
            return Ok(
                json!({"type":"ProviderSubmissionCount","count":authority.provider_submission_count()}),
            )
        }
        "__LoseAdapterDedupState" => {
            authority.lose_adapter_dedup_state();
            return Ok(json!({"type":"AdapterDedupLost"}));
        }
        _ => return Err("unknown syscall operation".into()),
    };
    Ok(outcome_value(governed))
}

fn serve_connection(
    mut stream: UnixStream,
    authority: Arc<Mutex<D1GovernedTurnAuthority>>,
    child: Arc<Mutex<Option<Child>>>,
) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    loop {
        let payload = match read_framed(&mut stream) {
            Ok(payload) => payload,
            Err(error) if error.kind() == ErrorKind::InvalidData => {
                let _ = write_framed(
                    &mut stream,
                    &serde_json::to_vec(&malformed(String::new(), error.to_string())).unwrap(),
                );
                return;
            }
            Err(_) => return,
        };
        let request: SyscallRequest = match serde_json::from_slice(&payload) {
            Ok(value) => value,
            Err(error) => {
                let _ = write_framed(
                    &mut stream,
                    &serde_json::to_vec(&malformed(String::new(), error.to_string())).unwrap(),
                );
                return;
            }
        };
        let outcome = dispatch(
            &mut authority.lock().expect("authority mutex poisoned"),
            &request,
        );
        let fenced = matches!(outcome.as_ref().ok(), Some(value) if value.get("type") == Some(&json!("GenerationFenced")));
        let response = match outcome {
            Ok(outcome) => SyscallResponse {
                request_id: request.request_id,
                status: "Ok".into(),
                outcome: Some(outcome),
                error: None,
            },
            Err(error) => malformed(request.request_id, error),
        };
        let _ = write_framed(
            &mut stream,
            &serde_json::to_vec(&response).expect("response JSON"),
        );
        if fenced {
            if let Some(mut child) = child.lock().expect("child mutex poisoned").take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn run(config: Config) -> io::Result<()> {
    fs::create_dir_all(&config.storage_root)?;
    fs::set_permissions(&config.storage_root, fs::Permissions::from_mode(0o700))?;
    let authority = Arc::new(Mutex::new(D1GovernedTurnAuthority::open(
        &config.storage_root,
    )?));
    let listener = bind_socket(&config.socket)?;
    let child = Arc::new(Mutex::new(match config.child {
        Some(command) => Some(
            Command::new("sh")
                .arg("-c")
                .arg(command)
                .env("CASTOR_IPC_SOCKET", &config.socket)
                .spawn()?,
        ),
        None => None,
    }));
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let authority = Arc::clone(&authority);
                let child = Arc::clone(&child);
                thread::spawn(move || serve_connection(stream, authority, child));
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

/// Compatibility-only read path retained for the earlier vertical-slice
/// validation command.  It never opens a second writer or starts a listener.
fn legacy_check(state_dir: PathBuf) -> io::Result<()> {
    let core_root = state_dir.join("core");
    let adapter_root = state_dir.join("adapter");
    if !core_root.join("regions").is_dir() || !adapter_root.join("adapter-config.json").is_file() {
        return Err(io::Error::new(
            ErrorKind::NotFound,
            "castord state root is not initialized",
        ));
    }
    D1DurableStorage::inspect(&core_root)?;
    let identity = inspect_adapter_store(&adapter_root, &core_root)?;
    println!(
        "castord state valid: adapter={} assurance_profile={}",
        identity.adapter_id, identity.assurance_profile
    );
    Ok(())
}

fn main() {
    let arguments: Vec<_> = env::args().collect();
    let result =
        if arguments.len() == 4 && arguments[1] == "--state-dir" && arguments[3] == "--check" {
            legacy_check(PathBuf::from(&arguments[2]))
        } else {
            parse_args()
                .map_err(|error| io::Error::new(ErrorKind::InvalidInput, error))
                .and_then(run)
        };
    if let Err(error) = result {
        eprintln!("{error}\n{}", usage());
        std::process::exit(2);
    }
}
