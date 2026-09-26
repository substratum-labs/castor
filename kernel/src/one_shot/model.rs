//! Host-owned buffered model bridge. The C-03 authority path is always real.

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

pub struct SocketModelService {
    stop: Arc<AtomicBool>,
    failed: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl SocketModelService {
    pub fn start(control_socket: PathBuf, model_socket: PathBuf, timeout: Duration) -> Self {
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
                    if report_buffered_result(&control_socket, &model_socket, request, timeout)
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

    pub fn has_failed(&self) -> bool {
        self.failed.load(Ordering::SeqCst)
    }
}

impl Drop for SocketModelService {
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
    request: &Value,
    timeout: Duration,
) -> io::Result<()> {
    let interaction_id = required_str(request, "interaction_id")?;
    let expected_request_digest = required_str(request, "request_digest")?;
    let mut control = GatewayClient::connect(control_socket)?;
    let read = control.request(&SyscallRequest {
        request_id: format!("read-model-request-{interaction_id}"),
        op: "ReadModelRequest".to_owned(),
        payload: json!({"interaction_id": interaction_id}),
    })?;
    if read.status != "Ok" {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "model request Region is missing or inaccessible",
        ));
    }
    let region = read
        .outcome
        .ok_or_else(|| io::Error::other("missing model request Region"))?;
    if required_str(&region, "region_ref")? != format!("region://model-request/{interaction_id}") {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "model request Region identity mismatch",
        ));
    }
    let content: Vec<u8> =
        serde_json::from_value(region.get("content").cloned().unwrap_or(Value::Null))
            .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
    let actual_digest = format!("sha256:{:x}", Sha256::digest(&content));
    if required_str(&region, "content_digest")? != actual_digest
        || expected_request_digest != actual_digest
    {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "model request digest mismatch",
        ));
    }
    let full_request: Value = serde_json::from_slice(&content)
        .map_err(|error| io::Error::new(ErrorKind::InvalidData, error))?;
    if full_request["schema_version"] != 1
        || full_request["interaction_id"] != interaction_id
        || full_request["messages"]
            .as_array()
            .is_none_or(|messages| messages.is_empty())
        || !full_request["tools"].is_array()
        || serde_json::to_vec(&full_request).map_err(io::Error::other)? != content
    {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "invalid canonical model request",
        ));
    }
    for attempt in 0..3 {
        let outcome = (|| {
            let mut stream = UnixStream::connect(model_socket)?;
            stream.set_read_timeout(Some(timeout))?;
            stream.set_write_timeout(Some(timeout))?;
            let envelope = json!({
                "interaction_id": interaction_id,
                "request_digest": expected_request_digest,
                "request": full_request
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
