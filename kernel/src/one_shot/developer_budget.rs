//! Durable, bounded local-model admission shared by native and bridged entrypoints.
//! A request is reserved before the transport closure may perform provider I/O.
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, ErrorKind, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_REQUEST: usize = 2 * 1024 * 1024;
const MAX_RESPONSE: usize = 2 * 1024 * 1024;
const MAX_CALLS: usize = 3;
const MAX_OUTPUT_TOKENS: u64 = 512;

#[derive(Clone, Serialize)]
pub struct Reservation {
    pub ordinal: usize,
    pub interaction_id: String,
    pub request_sha256: String,
    pub status: &'static str,
    pub reserved_unix_ns: u128,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub error: Option<String>,
}

#[derive(Serialize)]
struct LedgerView<'a> {
    transport: &'static str,
    max_calls: usize,
    max_output_tokens_per_call: u64,
    reservations: &'a [Reservation],
}

/// Forward one reserved native request to the bundled trusted local adapter.
/// The caller owns the durable admission ledger and must invoke this at most once.
pub fn forward_to_local_adapter(
    socket: &Path,
    request: &[u8],
    timeout: Duration,
) -> io::Result<Vec<u8>> {
    if request.is_empty() || request.len() > MAX_REQUEST {
        return Err(invalid("adapter request exceeds cap"));
    }
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    stream.write_all(&(request.len() as u32).to_be_bytes())?;
    stream.write_all(request)?;
    let mut header = [0u8; 4];
    stream.read_exact(&mut header)?;
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > MAX_RESPONSE {
        return Err(invalid("adapter response exceeds cap"));
    }
    let mut response = vec![0; size];
    stream.read_exact(&mut response)?;
    Ok(response)
}

pub struct BudgetLedger {
    root: PathBuf,
    records: Vec<Reservation>,
    completed: Vec<Vec<u8>>,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn write_atomic_new(path: &Path, bytes: &[u8]) -> io::Result<()> {
    if path.exists() {
        return Err(io::Error::new(
            ErrorKind::AlreadyExists,
            "bridge response already exists",
        ));
    }
    let temporary = path.with_extension("writing");
    write_exclusive(&temporary, bytes)?;
    fs::rename(&temporary, path)?;
    File::open(
        path.parent()
            .ok_or_else(|| invalid("missing bridge parent"))?,
    )?
    .sync_all()
}

fn read_regular_bounded(path: &Path, cap: usize) -> io::Result<Vec<u8>> {
    if !fs::symlink_metadata(path)?.file_type().is_file() {
        return Err(invalid("bridge request must be a regular file"));
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let mut bytes = Vec::new();
    file.take((cap + 1) as u64).read_to_end(&mut bytes)?;
    if bytes.len() > cap {
        return Err(invalid("bridge request exceeds cap"));
    }
    Ok(bytes)
}

fn write_exclusive(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    File::open(
        path.parent()
            .ok_or_else(|| invalid("missing evidence parent"))?,
    )?
    .sync_all()
}

impl BudgetLedger {
    pub fn create(root: &Path) -> io::Result<Self> {
        fs::create_dir(root)?;
        let ledger = Self {
            root: root.to_owned(),
            records: Vec::new(),
            completed: Vec::new(),
        };
        let bytes = serde_json::to_vec(&ledger.view()).map_err(io::Error::other)?;
        write_exclusive(&root.join("budget.json"), &bytes)?;
        Ok(ledger)
    }

    fn view(&self) -> LedgerView<'_> {
        LedgerView {
            transport: "LOCAL_OLLAMA",
            max_calls: MAX_CALLS,
            max_output_tokens_per_call: MAX_OUTPUT_TOKENS,
            reservations: &self.records,
        }
    }

    fn persist(&self) -> io::Result<()> {
        let temporary = self.root.join("budget.json.writing");
        write_exclusive(
            &temporary,
            &serde_json::to_vec(&self.view()).map_err(io::Error::other)?,
        )?;
        fs::rename(&temporary, self.root.join("budget.json"))?;
        File::open(&self.root)?.sync_all()
    }

    pub fn process<F>(&mut self, raw: &[u8], transport: F) -> io::Result<Vec<u8>>
    where
        F: FnOnce(&[u8]) -> io::Result<Vec<u8>>,
    {
        if raw.is_empty() || raw.len() > MAX_REQUEST {
            return Err(invalid("model request exceeds cap"));
        }
        let request: Value =
            serde_json::from_slice(raw).map_err(|_| invalid("invalid model envelope"))?;
        let id = request
            .get("interaction_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty() && id.len() <= 160)
            .ok_or_else(|| invalid("invalid interaction ID"))?;
        let native = request
            .get("request")
            .filter(|value| value.is_object())
            .ok_or_else(|| invalid("invalid model envelope"))?;
        if native.get("schema_version").and_then(Value::as_u64) != Some(1)
            || native.get("interaction_id").and_then(Value::as_str) != Some(id)
            || native
                .get("messages")
                .and_then(Value::as_array)
                .is_none_or(Vec::is_empty)
            || native.get("tools").and_then(Value::as_array).is_none()
        {
            return Err(invalid("invalid native model request"));
        }
        let canonical = serde_json::to_vec(native).map_err(io::Error::other)?;
        let expected = format!("sha256:{:x}", Sha256::digest(&canonical));
        if request.get("request_digest").and_then(Value::as_str) != Some(expected.as_str()) {
            return Err(invalid("native model request digest mismatch"));
        }
        let digest = format!("sha256:{:x}", Sha256::digest(raw));
        if let Some((index, prior)) = self
            .records
            .iter()
            .enumerate()
            .find(|(_, r)| r.interaction_id == id)
        {
            return if prior.request_sha256 == digest && prior.status == "COMPLETED" {
                Ok(self.completed[index].clone())
            } else {
                Err(invalid("duplicate interaction changed or incomplete"))
            };
        }
        if self.records.len() >= MAX_CALLS {
            return Err(invalid("unique model call budget exhausted"));
        }
        let ordinal = self.records.len() + 1;
        write_exclusive(&self.root.join(format!("request-{ordinal}.raw")), raw)?;
        self.records.push(Reservation {
            ordinal,
            interaction_id: id.to_owned(),
            request_sha256: digest,
            status: "RESERVED",
            reserved_unix_ns: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(io::Error::other)?
                .as_nanos(),
            input_tokens: None,
            output_tokens: None,
            error: None,
        });
        self.completed.push(Vec::new());
        self.persist()?; // Must commit the reservation before transport invocation.
        let delivered = transport(raw).and_then(|response| {
            if response.is_empty() || response.len() > MAX_RESPONSE {
                return Err(invalid("model response exceeds cap"));
            }
            let value: Value =
                serde_json::from_slice(&response).map_err(|_| invalid("invalid model response"))?;
            if value.get("interaction_id").and_then(Value::as_str) != Some(id) {
                return Err(invalid("model response interaction mismatch"));
            }
            let content: Vec<u8> =
                serde_json::from_value(value.get("content").cloned().unwrap_or(Value::Null))
                    .map_err(|_| invalid("invalid model response content"))?;
            let body: Value = serde_json::from_slice(&content)
                .map_err(|_| invalid("invalid model observation"))?;
            let input = body
                .pointer("/usage/input")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid("missing input usage"))?;
            let output = body
                .pointer("/usage/output")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid("missing output usage"))?;
            if output > MAX_OUTPUT_TOKENS
                || value.get("observation_digest").and_then(Value::as_str)
                    != Some(format!("sha256:{:x}", Sha256::digest(&content)).as_str())
            {
                return Err(invalid("model usage or observation digest mismatch"));
            }
            Ok((response, input, output))
        });
        match delivered {
            Ok((response, input, output)) => {
                write_exclusive(
                    &self.root.join(format!("response-{ordinal}.raw")),
                    &response,
                )?;
                self.records[ordinal - 1].input_tokens = Some(input);
                self.records[ordinal - 1].output_tokens = Some(output);
                self.records[ordinal - 1].status = "COMPLETED";
                self.completed[ordinal - 1] = response.clone();
                self.persist()?;
                Ok(response)
            }
            Err(error) => {
                self.records[ordinal - 1].status = "FAILED";
                self.records[ordinal - 1].error = Some(error.to_string());
                self.persist()?;
                Err(error)
            }
        }
    }

    /// Process only complete, atomically published requests in this private exchange.
    /// The caller supplies the bounded local adapter transport; no network is used here.
    pub fn process_bridge_once<F>(&mut self, bridge: &Path, mut transport: F) -> io::Result<usize>
    where
        F: FnMut(&[u8]) -> io::Result<Vec<u8>>,
    {
        let mut requests = Vec::new();
        for entry in fs::read_dir(bridge)? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            let Some(nonce) = name
                .strip_prefix("request-")
                .and_then(|s| s.strip_suffix(".json"))
            else {
                continue;
            };
            if nonce.len() != 32 || !nonce.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Err(invalid("invalid private bridge nonce"));
            }
            requests.push((name.to_string(), nonce.to_owned()));
        }
        requests.sort();
        let mut processed = 0;
        for (name, nonce) in requests {
            let response = bridge.join(format!("response-{nonce}.json"));
            let error = bridge.join(format!("error-{nonce}.json"));
            if response.exists() || error.exists() {
                continue;
            }
            let raw = read_regular_bounded(&bridge.join(name), MAX_REQUEST)?;
            match self.process(&raw, |body| transport(body)) {
                Ok(bytes) => write_atomic_new(&response, &bytes)?,
                Err(problem) => write_atomic_new(
                    &error,
                    &serde_json::to_vec(&json!({"error":problem.to_string()}))
                        .map_err(io::Error::other)?,
                )?,
            }
            processed += 1;
        }
        Ok(processed)
    }

    pub fn count(&self) -> usize {
        self.records.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::Cell;

    fn request(id: &str) -> Vec<u8> {
        let request = json!({"schema_version":1,"interaction_id":id,"messages":[{"role":"user","content":"task"}],"tools":[]});
        let digest = format!(
            "sha256:{:x}",
            Sha256::digest(serde_json::to_vec(&request).unwrap())
        );
        serde_json::to_vec(&json!({"interaction_id":id,"request_digest":digest,"request":request}))
            .unwrap()
    }
    fn answer(id: &str, output: u64) -> Vec<u8> {
        let content = serde_json::to_vec(
            &json!({"content":[],"stopReason":"stop","usage":{"input":7,"output":output}}),
        )
        .unwrap();
        serde_json::to_vec(&json!({"interaction_id":id,"observation_digest":format!("sha256:{:x}",Sha256::digest(&content)),"content":content})).unwrap()
    }

    #[test]
    fn reserve_before_transport_and_refuse_duplicate_or_extra_post() {
        let root = tempfile::tempdir().unwrap();
        let mut gate = BudgetLedger::create(&root.path().join("budget")).unwrap();
        let calls = Cell::new(0);
        for n in 1..=3 {
            let id = format!("interaction-{n}");
            let raw = request(&id);
            let first = gate
                .process(&raw, |_: &[u8]| {
                    let ledger: Value =
                        serde_json::from_slice(&fs::read(root.path().join("budget/budget.json"))?)?;
                    assert_eq!(ledger["reservations"][n - 1]["status"], "RESERVED");
                    calls.set(calls.get() + 1);
                    Ok(answer(&id, 40))
                })
                .unwrap();
            assert_eq!(
                gate.process(&raw, |_| panic!("duplicate transport"))
                    .unwrap(),
                first
            );
        }
        assert_eq!(calls.get(), 3);
        assert!(gate
            .process(&request("interaction-4"), |_| panic!("fourth transport"))
            .is_err());
        assert_eq!(gate.count(), 3);
        assert!(BudgetLedger::create(&root.path().join("budget")).is_err());
    }

    #[test]
    fn local_adapter_transport_uses_one_bounded_frame_each_way() {
        use std::os::unix::net::UnixListener;
        let root = tempfile::tempdir().unwrap();
        let socket = root.path().join("adapter.sock");
        let server = UnixListener::bind(&socket).unwrap();
        let expected = request("interaction-1");
        let reply = answer("interaction-1", 12);
        let worker = std::thread::spawn({
            let expected = expected.clone();
            let reply = reply.clone();
            move || {
                let (mut stream, _) = server.accept().unwrap();
                let mut header = [0u8; 4];
                stream.read_exact(&mut header).unwrap();
                assert_eq!(u32::from_be_bytes(header) as usize, expected.len());
                let mut body = vec![0; expected.len()];
                stream.read_exact(&mut body).unwrap();
                assert_eq!(body, expected);
                stream
                    .write_all(&(reply.len() as u32).to_be_bytes())
                    .unwrap();
                stream.write_all(&reply).unwrap();
            }
        });
        let actual = forward_to_local_adapter(&socket, &expected, Duration::from_secs(2)).unwrap();
        assert_eq!(actual, reply);
        worker.join().unwrap();
    }

    #[test]
    fn private_file_exchange_replays_without_second_provider_post() {
        let root = tempfile::tempdir().unwrap();
        let bridge = root.path().join("bridge");
        fs::create_dir(&bridge).unwrap();
        let mut gate = BudgetLedger::create(&root.path().join("budget")).unwrap();
        let raw = request("interaction-1");
        fs::write(
            bridge.join("request-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"),
            &raw,
        )
        .unwrap();
        let calls = Cell::new(0);
        let first = gate
            .process_bridge_once(&bridge, |_: &[u8]| {
                calls.set(calls.get() + 1);
                Ok(answer("interaction-1", 15))
            })
            .unwrap();
        assert_eq!(first, 1);
        fs::write(
            bridge.join("request-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json"),
            &raw,
        )
        .unwrap();
        let second = gate
            .process_bridge_once(&bridge, |_| panic!("duplicate POST"))
            .unwrap();
        assert_eq!(second, 1);
        assert_eq!(calls.get(), 1);
        assert_eq!(
            fs::read(bridge.join("response-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json")).unwrap(),
            answer("interaction-1", 15),
        );
    }

    #[test]
    fn invalid_native_digest_never_reserves_or_reaches_provider() {
        let root = tempfile::tempdir().unwrap();
        let mut gate = BudgetLedger::create(&root.path().join("budget")).unwrap();
        let mut request: Value = serde_json::from_slice(&request("interaction-1")).unwrap();
        request["request_digest"] = json!("sha256:forged");
        let raw = serde_json::to_vec(&request).unwrap();
        assert!(gate
            .process(&raw, |_| panic!("invalid request reached provider"))
            .is_err());
        assert_eq!(gate.count(), 0);
    }

    #[test]
    fn failed_or_changed_interaction_spends_reservation_without_retry() {
        let root = tempfile::tempdir().unwrap();
        let mut gate = BudgetLedger::create(&root.path().join("budget")).unwrap();
        let raw = request("interaction-1");
        assert!(gate
            .process(&raw, |_| Err(io::Error::other("provider lost")))
            .is_err());
        assert!(gate.process(&raw, |_| panic!("retry POST")).is_err());
        let view: Value =
            serde_json::from_slice(&fs::read(root.path().join("budget/budget.json")).unwrap())
                .unwrap();
        assert_eq!(view["reservations"][0]["status"], "FAILED");
        let mut invalid = request("interaction-2");
        invalid.push(b' ');
        assert!(gate
            .process(&invalid, |_| Ok(answer("interaction-2", 513)))
            .is_err());
        assert!(gate
            .process(&invalid, |_| panic!("retry malformed usage"))
            .is_err());
        assert_eq!(gate.count(), 2);
    }
}
