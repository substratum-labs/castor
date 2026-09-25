//! Test-only external model seam. The C-03 authority path is always real.

use crate::host::{read_framed, write_framed, GatewayClient, SyscallRequest};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::io::{self, ErrorKind};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

pub struct TestModelService {
    stop: Arc<AtomicBool>,
    failed: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl TestModelService {
    pub fn start(control_socket: PathBuf, model_socket: PathBuf, prompt: String) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let failed = Arc::new(AtomicBool::new(false));
        let worker_stop = stop.clone();
        let worker_failed = failed.clone();
        let worker = thread::spawn(move || {
            let mut seen = HashSet::new();
            while !worker_stop.load(Ordering::SeqCst) {
                let Ok(mut control) = GatewayClient::connect(&control_socket) else {
                    thread::sleep(Duration::from_millis(10));
                    continue;
                };
                let Ok(response) = control.request(&SyscallRequest {
                    request_id: "inspect-model-interactions".to_owned(),
                    op: "InspectJournal".to_owned(),
                    payload: json!({}),
                }) else {
                    thread::sleep(Duration::from_millis(10));
                    continue;
                };
                let Some(entries) = response
                    .outcome
                    .as_ref()
                    .and_then(|outcome| outcome.get("entries"))
                    .and_then(Value::as_array)
                else {
                    thread::sleep(Duration::from_millis(10));
                    continue;
                };
                for entry in entries {
                    let Some(request) = entry.get("InteractionRequested") else {
                        continue;
                    };
                    let Some(id) = request.get("interaction_id").and_then(Value::as_str) else {
                        continue;
                    };
                    if !seen.insert(id.to_owned()) {
                        continue;
                    }
                    if report_buffered_result(&control_socket, &model_socket, &prompt, request)
                        .is_err()
                    {
                        worker_failed.store(true, Ordering::SeqCst);
                    }
                }
                thread::sleep(Duration::from_millis(10));
            }
        });
        Self {
            stop,
            failed,
            worker: Some(worker),
        }
    }

    pub fn finish(mut self) -> bool {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
        self.failed.load(Ordering::SeqCst)
    }
}

impl Drop for TestModelService {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn report_buffered_result(
    control_socket: &PathBuf,
    model_socket: &PathBuf,
    prompt: &str,
    request: &Value,
) -> io::Result<()> {
    let interaction_id = required_str(request, "interaction_id")?;
    let expected_request_digest = format!("sha256:{:x}", Sha256::digest(prompt.as_bytes()));
    if required_str(request, "request_digest")? != expected_request_digest {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "model request digest mismatch",
        ));
    }
    for attempt in 0..3 {
        let outcome = (|| {
            let mut stream = UnixStream::connect(model_socket)?;
            stream.set_read_timeout(Some(Duration::from_secs(2)))?;
            stream.set_write_timeout(Some(Duration::from_secs(2)))?;
            let envelope = json!({
                "interaction_id": interaction_id,
                "request_digest": expected_request_digest,
                "prompt": prompt
            });
            write_framed(
                &mut stream,
                &serde_json::to_vec(&envelope).map_err(io::Error::other)?,
            )?;
            let response: Value = serde_json::from_slice(&read_framed(&mut stream)?)
                .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
            if required_str(&response, "interaction_id")? != interaction_id {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    "model interaction ID mismatch",
                ));
            }
            let region = required_str(&response, "observation_region_id")?;
            let content: Vec<u8> =
                serde_json::from_value(response.get("content").cloned().unwrap_or(Value::Null))
                    .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
            let digest = format!("sha256:{:x}", Sha256::digest(&content));
            if required_str(&response, "observation_digest")? != digest {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    "model result digest mismatch",
                ));
            }
            let mut control = GatewayClient::connect(control_socket)?;
            let persisted = control.request(&SyscallRequest {
                request_id: format!("model-region-{interaction_id}"),
                op: "EnsureRegion".to_owned(),
                payload: json!({
                    "region_ref": region,
                    "content_digest": digest,
                    "content": content,
                    "profile": "D1"
                }),
            })?;
            if persisted
                .outcome
                .as_ref()
                .and_then(|value| value.get("type"))
                != Some(&json!("Success"))
            {
                return Err(io::Error::other("model result Region was not persisted"));
            }
            let bound = control.request(&SyscallRequest {
                request_id: format!("model-bind-{interaction_id}"),
                op: "ReportOutcome".to_owned(),
                payload: json!({
                    "interaction_id": interaction_id,
                    "observation_region_id": region,
                    "observation_digest": digest
                }),
            })?;
            if bound.outcome.as_ref().and_then(|value| value.get("type"))
                != Some(&json!("InteractionBound"))
            {
                return Err(io::Error::other("model result was not bound"));
            }
            Ok(())
        })();
        if outcome.is_ok() {
            return Ok(());
        }
        if attempt < 2 {
            thread::sleep(Duration::from_millis(20));
        }
    }
    Err(io::Error::other(
        "model provider failed after three attempts",
    ))
}

fn required_str<'a>(value: &'a Value, key: &str) -> io::Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, format!("missing {key}")))
}
