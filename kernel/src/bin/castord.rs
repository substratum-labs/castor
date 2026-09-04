use castor_kernel::c01_storage::{D1DurableStorage, DurabilityProfile, EnsureRegionOutcome};
use castor_kernel::c04_adapter::inspect_adapter_store;
use castor_kernel::c06_composition::*;
use castor_kernel::host::{
    read_framed, write_framed, GatewayError, SyscallRequest, SyscallResponse,
};
use castor_kernel::sandbox::{
    build_castor_untrusted_agent_config, RocheProcessSupervisor, RocheSandboxRunner,
    DEFAULT_SANDBOX_IMAGE,
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
    "usage: castord --storage-root PATH --socket PATH [--control-socket PATH] [--child CMD] [--sandbox roche|none] [--sandbox-image IMAGE]"
}

struct Config {
    storage_root: PathBuf,
    socket: PathBuf,
    control_socket: Option<PathBuf>,
    child: Option<String>,
    sandbox: SandboxMode,
    sandbox_image: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SandboxMode {
    None,
    Roche,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SocketChannel {
    Agent,
    Control,
}

enum SupervisedChild {
    Bare(Child),
    Roche(RocheProcessSupervisor),
}

impl SupervisedChild {
    fn kill_immediate(&mut self) {
        match self {
            Self::Bare(child) => {
                let _ = child.kill();
                let _ = child.wait();
            }
            Self::Roche(supervisor) => {
                let _ = supervisor.kill_immediate();
            }
        }
    }
}

fn parse_args() -> Result<Config, String> {
    let mut args = env::args().skip(1);
    let mut storage_root = None;
    let mut socket = None;
    let mut control_socket = None;
    let mut child = None;
    let mut sandbox = SandboxMode::None;
    let mut sandbox_image = DEFAULT_SANDBOX_IMAGE.to_owned();
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
            "--control-socket" => {
                control_socket =
                    Some(PathBuf::from(args.next().ok_or_else(|| {
                        "--control-socket requires a path".to_string()
                    })?))
            }
            "--child" => {
                child = Some(
                    args.next()
                        .ok_or_else(|| "--child requires a command".to_string())?,
                )
            }
            "--sandbox" => {
                sandbox = match args
                    .next()
                    .ok_or_else(|| "--sandbox requires roche or none".to_string())?
                    .as_str()
                {
                    "none" => SandboxMode::None,
                    "roche" => SandboxMode::Roche,
                    other => return Err(format!("unsupported sandbox mode: {other}")),
                }
            }
            "--sandbox-image" => {
                sandbox_image = args
                    .next()
                    .ok_or_else(|| "--sandbox-image requires an image".to_string())?
            }
            _ => return Err(format!("unknown argument: {arg}")),
        }
    }
    Ok(Config {
        storage_root: storage_root.ok_or_else(|| "--storage-root is required".to_string())?,
        socket: socket.ok_or_else(|| "--socket is required".to_string())?,
        control_socket,
        child,
        sandbox,
        sandbox_image,
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

fn gateway_error(request_id: String, code: &str, message: impl Into<String>) -> SyscallResponse {
    SyscallResponse {
        request_id,
        status: "Error".into(),
        outcome: None,
        error: Some(GatewayError {
            code: code.into(),
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
            GovernedTurnOutcome::EntryPersisted => "EntryPersisted", GovernedTurnOutcome::Admitted => "Admitted", GovernedTurnOutcome::InteractionRequested => "InteractionRequested", GovernedTurnOutcome::InteractionBound => "InteractionBound", GovernedTurnOutcome::InteractionConsumed => "InteractionConsumed", GovernedTurnOutcome::TurnCommitted => "TurnCommitted", GovernedTurnOutcome::ActionRegistered => "ActionRegistered", GovernedTurnOutcome::DispatchRecorded => "DispatchRecorded", GovernedTurnOutcome::Delivered => "Delivered", GovernedTurnOutcome::DuplicateDelivery => "DuplicateDelivery", GovernedTurnOutcome::QuarantinedDispute => "QuarantinedDispute", GovernedTurnOutcome::CapabilityGranted => "CapabilityGranted", GovernedTurnOutcome::CapabilityRevoked => "CapabilityRevoked", GovernedTurnOutcome::Reconstructed => "Reconstructed", GovernedTurnOutcome::Ambiguous => "Ambiguous", GovernedTurnOutcome::RejectedStaleAuthority => "RejectedStaleAuthority", GovernedTurnOutcome::RejectedCapabilityRevoked => "RejectedCapabilityRevoked", GovernedTurnOutcome::RejectedInvalidProofClass => "RejectedInvalidProofClass", GovernedTurnOutcome::RejectedLateOrClosedTurn => "RejectedLateOrClosedTurn", GovernedTurnOutcome::RejectedCurrentState => "RejectedCurrentState", GovernedTurnOutcome::RejectedNotFound => "RejectedNotFound", GovernedTurnOutcome::RejectedPrecondition => "RejectedPrecondition", GovernedTurnOutcome::IntegrityOrProtocolFault => "IntegrityOrProtocolFault", GovernedTurnOutcome::UnavailableBeforeAck => "UnavailableBeforeAck", _ => unreachable!() }}),
    }
}
fn ensure_value(outcome: EnsureRegionOutcome) -> Value {
    json!({"type": match outcome { EnsureRegionOutcome::Success(_) => "Success", EnsureRegionOutcome::AlreadyPersistedSameContent(_) => "AlreadyPersistedSameContent", EnsureRegionOutcome::RejectedIdentityConflict => "RejectedIdentityConflict", EnsureRegionOutcome::UnavailableBeforeAck => "UnavailableBeforeAck", EnsureRegionOutcome::IntegrityFault => "IntegrityFault" }})
}

fn dispatch(
    authority: &mut D1GovernedTurnAuthority,
    request: &SyscallRequest,
    channel: SocketChannel,
) -> Result<Value, String> {
    let agent_allowed = matches!(
        request.op.as_str(),
        "AdmitTurn"
            | "CommitTurn"
            | "RegisterAction"
            | "PresentAdmissionCertificate"
            | "RecordDispatchAttempt"
            | "DeliverArmedAttempt"
            | "PresentSettlementCertificate"
            | "PersistFence"
            | "RevokeCapability"
            | "Replay"
            | "EnsureRegion"
            | "__ProviderSubmissionCount"
            | "__LoseAdapterDedupState"
            | "RequestInteraction"
            | "ReportOutcome"
            | "ConsumeInteraction"
    );
    let control_allowed = matches!(
        request.op.as_str(),
        "GrantCapability"
            | "RevokeCapability"
            | "ResolveQuarantinedDispute"
            | "PersistFence"
            | "GetProjectionSummary"
            | "InspectJournal"
    );
    if (channel == SocketChannel::Agent && !agent_allowed)
        || (channel == SocketChannel::Control && !control_allowed)
    {
        return Err("UnauthorizedOpcode".into());
    }
    let p = &request.payload;
    let governed = match request.op.as_str() {
        "GrantCapability" => {
            let request: GrantCapabilityRequest =
                serde_json::from_value(p.clone()).map_err(|error| error.to_string())?;
            authority.grant_capability(request)
        }
        "ResolveQuarantinedDispute" => {
            let request: ResolveQuarantinedDisputeRequest =
                serde_json::from_value(p.clone()).map_err(|error| error.to_string())?;
            return Ok(outcome_value(
                authority
                    .resolve_quarantined_dispute(request)
                    .unwrap_or_else(|outcome| outcome),
            ));
        }
        "GetProjectionSummary" => return Ok(authority.projection_summary()),
        "InspectJournal" => return Ok(json!({"entries": authority.inspect_journal()})),
        "AdmitTurn" => authority.admit_turn(AdmitTurnRequest {
            agent_id: string(p, "agent_id")?,
            turn_id: number(p, "turn_id")?,
            lease_epoch: number(p, "lease_epoch")?,
            base_projection_digest: string(p, "base_projection_digest")?,
            cap_id: p.get("cap_id").and_then(Value::as_str).map(str::to_owned),
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
            cap_id: p.get("cap_id").and_then(Value::as_str).map(str::to_owned),
        }),
        "RegisterAction" => authority.register_action(ActionRegistrationRequest {
            action_id: string(p, "action_id")?,
            agent_id: p
                .get("agent_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .into(),
            action_family: p
                .get("action_family")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .into(),
            cap_id: p
                .get("cap_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .into(),
            target_scope: p
                .get("target_scope")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .into(),
            numeric_parameters: Default::default(),
            exact_parameters: Default::default(),
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
        "RevokeCapability" => match channel {
            SocketChannel::Agent => authority.revoke_capability_with_authorization(
                p.get("authorization_capability_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
                &string(p, "capability_id")?,
            ),
            SocketChannel::Control => authority.revoke_capability(&string(p, "capability_id")?),
        },
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
    child: Arc<Mutex<Option<SupervisedChild>>>,
    channel: SocketChannel,
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
            channel,
        );
        let fenced = matches!(outcome.as_ref().ok(), Some(value) if value.get("type") == Some(&json!("GenerationFenced")));
        let response = match outcome {
            Ok(outcome) => SyscallResponse {
                request_id: request.request_id,
                status: "Ok".into(),
                outcome: Some(outcome),
                error: None,
            },
            Err(error) if error == "UnauthorizedOpcode" => {
                gateway_error(request.request_id, "UnauthorizedOpcode", error)
            }
            Err(error) => malformed(request.request_id, error),
        };
        let _ = write_framed(
            &mut stream,
            &serde_json::to_vec(&response).expect("response JSON"),
        );
        if fenced {
            if let Some(mut child) = child.lock().expect("child mutex poisoned").take() {
                // `persist_fence` does the C-01 append and sync before this
                // branch is reached.  A Roche carrier therefore gets SIGKILL
                // with no grace window after the durable fence.
                child.kill_immediate();
            }
        }
    }
}

fn serve_listener(
    listener: UnixListener,
    authority: Arc<Mutex<D1GovernedTurnAuthority>>,
    child: Arc<Mutex<Option<SupervisedChild>>>,
    channel: SocketChannel,
) -> io::Result<()> {
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let authority = Arc::clone(&authority);
                let child = Arc::clone(&child);
                thread::spawn(move || serve_connection(stream, authority, child, channel));
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn run(config: Config) -> io::Result<()> {
    fs::create_dir_all(&config.storage_root)?;
    fs::set_permissions(&config.storage_root, fs::Permissions::from_mode(0o700))?;
    let authority = Arc::new(Mutex::new(D1GovernedTurnAuthority::open(
        &config.storage_root,
    )?));
    let listener = bind_socket(&config.socket)?;
    let control_listener = config
        .control_socket
        .as_deref()
        .map(bind_socket)
        .transpose()?;
    let child = Arc::new(Mutex::new(match config.child {
        Some(command) if config.sandbox == SandboxMode::Roche => {
            let profile = build_castor_untrusted_agent_config(
                config.sandbox_image,
                &config.socket,
                None,
                None,
            )
            .map_err(|error| io::Error::new(ErrorKind::InvalidInput, error))?;
            Some(SupervisedChild::Roche(
                RocheSandboxRunner::new(profile).start(&command)?,
            ))
        }
        Some(command) => Some(SupervisedChild::Bare(
            Command::new("sh")
                .arg("-c")
                .arg(command)
                .env("CASTOR_IPC_SOCKET", &config.socket)
                .spawn()?,
        )),
        None => None,
    }));
    if let Some(control_listener) = control_listener {
        let authority = Arc::clone(&authority);
        let child = Arc::clone(&child);
        thread::spawn(move || {
            let _ = serve_listener(control_listener, authority, child, SocketChannel::Control);
        });
    }
    serve_listener(listener, authority, child, SocketChannel::Agent)
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
