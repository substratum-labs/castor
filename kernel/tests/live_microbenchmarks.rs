//! Explicitly-invoked physical microbenchmarks for T-312-C.
//!
//! Run with:
//! `cargo test --test live_microbenchmarks -- --ignored --nocapture`

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage, DurableStorage,
};
use castor_kernel::host::{GatewayClient, SyscallRequest};
use serde_json::{json, Value};
use std::fs;
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const UDS_ITERATIONS: usize = 1_000;
const JOURNAL_ENTRIES: usize = 2_000;

#[derive(Debug, Clone, Copy)]
struct LatencySummary {
    p50_us: f64,
    p95_us: f64,
    p99_us: f64,
    throughput_per_sec: f64,
}

fn summarize_latencies(samples: &[Duration], elapsed: Duration) -> LatencySummary {
    assert!(!samples.is_empty(), "latency sample set must not be empty");
    assert!(
        !elapsed.is_zero(),
        "benchmark elapsed time must be non-zero"
    );
    let mut sorted = samples.to_vec();
    sorted.sort_unstable();
    let percentile = |fraction: f64| {
        let rank = ((sorted.len() as f64 * fraction).ceil() as usize)
            .saturating_sub(1)
            .min(sorted.len() - 1);
        duration_us(sorted[rank])
    };
    LatencySummary {
        p50_us: percentile(0.50),
        p95_us: percentile(0.95),
        p99_us: percentile(0.99),
        throughput_per_sec: samples.len() as f64 / elapsed.as_secs_f64(),
    }
}

#[test]
fn percentile_summary_contract() {
    let samples = [
        Duration::from_micros(40),
        Duration::from_micros(10),
        Duration::from_micros(30),
        Duration::from_micros(20),
    ];
    let summary = summarize_latencies(&samples, Duration::from_millis(2));
    assert_eq!(summary.p50_us, 20.0);
    assert_eq!(summary.p95_us, 40.0);
    assert_eq!(summary.p99_us, 40.0);
    assert_eq!(summary.throughput_per_sec, 2_000.0);
}

#[test]
fn comparative_arm_json_contract() {
    let arm = comparative_arm_value(
        "Arm A",
        "Direct UDS",
        4,
        LatencySummary {
            p50_us: 20.0,
            p95_us: 40.0,
            p99_us: 40.0,
            throughput_per_sec: 2_000.0,
        },
        3.5,
    );
    assert_eq!(arm["arm"], "Arm A");
    assert_eq!(arm["transport"], "Direct UDS");
    assert_eq!(arm["iterations"], 4);
    assert_eq!(arm["p50_us"], 20.0);
    assert_eq!(arm["p95_us"], 40.0);
    assert_eq!(arm["p99_us"], 40.0);
    assert_eq!(arm["throughput_requests_per_sec"], 2_000.0);
    assert_eq!(arm["speedup_factor"], 3.5);
    assert!(arm.get("latency").is_none());
}

struct Daemon {
    child: Child,
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn spawn_castord(root: &Path, socket: &Path, control_socket: &Path) -> Daemon {
    let child = Command::new(env!("CARGO_BIN_EXE_castord"))
        .args([
            "--storage-root",
            root.to_str().expect("UTF-8 storage root"),
            "--socket",
            socket.to_str().expect("UTF-8 agent socket"),
            "--control-socket",
            control_socket.to_str().expect("UTF-8 control socket"),
        ])
        .spawn()
        .expect("launch physical castord");
    let deadline = Instant::now() + Duration::from_secs(3);
    while UnixStream::connect(socket).is_err() || UnixStream::connect(control_socket).is_err() {
        assert!(
            Instant::now() < deadline,
            "castord must listen within startup timeout"
        );
        thread::sleep(Duration::from_millis(10));
    }
    thread::sleep(Duration::from_millis(25));
    Daemon { child }
}

fn projection_summary_request(request_id: usize) -> SyscallRequest {
    SyscallRequest {
        request_id: format!("benchmark-{request_id}"),
        op: "GetProjectionSummary".into(),
        payload: json!({}),
    }
}

fn duration_us(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1_000_000.0
}

fn read_http_headers(stream: &mut TcpStream) -> io::Result<Vec<u8>> {
    let mut headers = Vec::with_capacity(512);
    let mut byte = [0_u8; 1];
    while !headers.ends_with(b"\r\n\r\n") {
        match stream.read_exact(&mut byte) {
            Ok(()) => headers.push(byte[0]),
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof && headers.is_empty() => {
                return Ok(Vec::new());
            }
            Err(error) => return Err(error),
        }
        if headers.len() > 16 * 1024 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "HTTP headers exceed 16 KiB",
            ));
        }
    }
    Ok(headers)
}

fn content_length(headers: &[u8]) -> io::Result<usize> {
    let headers = std::str::from_utf8(headers)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>())
        })
        .transpose()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "missing Content-Length"))
}

struct HttpReverseProxy {
    addr: SocketAddr,
    thread: Option<JoinHandle<io::Result<()>>>,
}

impl HttpReverseProxy {
    fn start(control_socket: &Path) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback HTTP proxy");
        let addr = listener.local_addr().expect("read HTTP proxy address");
        let control_socket = control_socket.to_owned();
        let thread = thread::spawn(move || {
            let (mut stream, _) = listener.accept()?;
            stream.set_nodelay(true)?;
            let mut backend = GatewayClient::connect(&control_socket)?;
            let mut request_id = 0_usize;
            loop {
                let headers = read_http_headers(&mut stream)?;
                if headers.is_empty() {
                    return Ok(());
                }
                if !headers.starts_with(b"GET /projection-summary HTTP/1.1\r\n") {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "unexpected HTTP proxy request",
                    ));
                }
                let response = backend.request(&projection_summary_request(request_id))?;
                request_id += 1;
                let body = serde_json::to_vec(&response)
                    .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n",
                    body.len()
                )?;
                stream.write_all(&body)?;
                stream.flush()?;
            }
        });
        Self {
            addr,
            thread: Some(thread),
        }
    }

    fn connect(&self) -> TcpStream {
        let stream = TcpStream::connect(self.addr).expect("connect loopback HTTP proxy");
        stream.set_nodelay(true).expect("set HTTP TCP_NODELAY");
        stream
    }
}

impl Drop for HttpReverseProxy {
    fn drop(&mut self) {
        if let Some(thread) = self.thread.take() {
            thread
                .join()
                .expect("HTTP reverse proxy thread panicked")
                .expect("HTTP reverse proxy failed");
        }
    }
}

fn http_projection_summary(stream: &mut TcpStream) -> Value {
    stream
        .write_all(
            b"GET /projection-summary HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n",
        )
        .expect("write HTTP proxy request");
    stream.flush().expect("flush HTTP proxy request");
    let headers = read_http_headers(stream).expect("read HTTP proxy response headers");
    assert!(
        headers.starts_with(b"HTTP/1.1 200 OK\r\n"),
        "HTTP proxy must return 200 OK"
    );
    let mut body = vec![0_u8; content_length(&headers).expect("HTTP response length")];
    stream
        .read_exact(&mut body)
        .expect("read HTTP proxy response body");
    serde_json::from_slice(&body).expect("parse HTTP proxy JSON response")
}

fn comparative_arm_value(
    arm: &str,
    transport: &str,
    iterations: usize,
    summary: LatencySummary,
    speedup_factor: f64,
) -> Value {
    json!({
        "arm": arm,
        "transport": transport,
        "iterations": iterations,
        "p50_us": summary.p50_us,
        "p95_us": summary.p95_us,
        "p99_us": summary.p99_us,
        "throughput_requests_per_sec": summary.throughput_per_sec,
        "speedup_factor": speedup_factor
    })
}

fn benchmark_comparative_rtt() -> Value {
    let root = tempfile::tempdir().expect("temporary castord benchmark root");
    let socket = root.path().join("castord.sock");
    let control_socket = root.path().join("control.sock");
    let _daemon = spawn_castord(root.path(), &socket, &control_socket);
    let mut uds_client = GatewayClient::connect(&control_socket).expect("connect control socket");

    for request_id in 0..50 {
        let response = uds_client
            .request(&projection_summary_request(request_id))
            .expect("warm-up AISA request");
        assert_eq!(response.status, "Ok");
    }

    let mut uds_samples = Vec::with_capacity(UDS_ITERATIONS);
    let uds_started = Instant::now();
    for request_id in 0..UDS_ITERATIONS {
        let started = Instant::now();
        let response = uds_client
            .request(&projection_summary_request(request_id))
            .expect("measured AISA request");
        let elapsed = started.elapsed();
        assert_eq!(response.status, "Ok");
        uds_samples.push(elapsed);
    }
    let uds_elapsed = uds_started.elapsed();
    let uds = summarize_latencies(&uds_samples, uds_elapsed);

    let proxy = HttpReverseProxy::start(&control_socket);
    let mut http_client = proxy.connect();
    for _ in 0..50 {
        let response = http_projection_summary(&mut http_client);
        assert_eq!(response["status"], "Ok");
    }
    let mut http_samples = Vec::with_capacity(UDS_ITERATIONS);
    let http_started = Instant::now();
    for _ in 0..UDS_ITERATIONS {
        let started = Instant::now();
        let response = http_projection_summary(&mut http_client);
        let elapsed = started.elapsed();
        assert_eq!(response["status"], "Ok");
        http_samples.push(elapsed);
    }
    let http_elapsed = http_started.elapsed();
    let http = summarize_latencies(&http_samples, http_elapsed);
    drop(http_client);
    drop(proxy);

    let uds_speedup = http.p50_us / uds.p50_us;

    json!({
        "operation": "GetProjectionSummary",
        "iterations_per_arm": UDS_ITERATIONS,
        "speedup_definition": "HTTP proxy p50 / direct UDS p50; HTTP is 1.0x baseline",
        "arms": [
            comparative_arm_value(
                "Arm A",
                "Direct UDS (control.sock)",
                UDS_ITERATIONS,
                uds,
                uds_speedup,
            ),
            comparative_arm_value(
                "Arm B",
                "HTTP/1.1 loopback reverse proxy -> UDS",
                UDS_ITERATIONS,
                http,
                1.0,
            )
        ]
    })
}

fn benchmark_crc32_journal() -> Value {
    let root = tempfile::tempdir().expect("temporary D1 benchmark root");
    let mut storage = D1DurableStorage::open(root.path()).expect("open D1 benchmark storage");
    let started = Instant::now();
    for entry_id in 1..=JOURNAL_ENTRIES as u64 {
        let outcome = storage.append_conditional(AppendConditionalRequest {
            agent_id: "benchmark-agent".into(),
            entry_id,
            expected_core_epoch: 1,
            expected_agent_generation: None,
            expected_turn_id: None,
            expected_lease_epoch: None,
            expected_base_projection_digest: None,
            entry: CoreEntry::CapabilityGranted {
                capability_id: format!("benchmark-capability-{entry_id}"),
                grant_json: r#"{"right":"BenchmarkAppend","scope":"t312"}"#.into(),
            },
            region_refs: Vec::new(),
        });
        assert!(
            matches!(outcome, AppendConditionalOutcome::EntryPersisted(_)),
            "benchmark append {entry_id} failed: {outcome:?}"
        );
    }
    let elapsed = started.elapsed();
    let bytes = fs::metadata(root.path().join("core-journal.log"))
        .expect("journal metadata")
        .len();
    let seconds = elapsed.as_secs_f64();
    let megabytes = bytes as f64 / 1_000_000.0;

    json!({
        "entries": JOURNAL_ENTRIES,
        "elapsed_ms": elapsed.as_secs_f64() * 1_000.0,
        "records_per_sec": JOURNAL_ENTRIES as f64 / seconds,
        "mb_per_sec": megabytes / seconds,
        "journal_bytes": bytes,
        "durability": "D1 sync_all per append",
        "checksum": "IEEE 802.3 CRC-32"
    })
}

fn write_results(results: &Value) {
    let results_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("repository root")
        .join("results");
    fs::create_dir_all(&results_dir).expect("create results directory");
    fs::write(
        results_dir.join("microbenchmarks.json"),
        serde_json::to_vec_pretty(results).expect("serialize benchmark JSON"),
    )
    .expect("write benchmark JSON");

    let comparative = &results["comparative_uds_http_rtt"];
    let direct = &comparative["arms"][0];
    let http = &comparative["arms"][1];
    let journal = &results["crc32_journal_framing_and_storage"];
    let markdown = format!(
        "# Castor Live Microbenchmarks\n\n\
         Generated at Unix time `{}` on `{}` / `{}` using the `{}` profile.\n\n\
         ## D-02 Direct UDS RTT\n\n\
         | Iterations | Operation | p50 (us) | p95 (us) | p99 (us) |\n\
         |---:|---|---:|---:|---:|\n\
         | {} | GetProjectionSummary | {:.3} | {:.3} | {:.3} |\n\n\
         ## CRC-32 Journal Framing and D1 Storage\n\n\
         | Entries | Total (ms) | Records/s | MB/s | Journal bytes |\n\
         |---:|---:|---:|---:|---:|\n\
         | {} | {:.3} | {:.3} | {:.3} | {} |\n\n\
         Each journal record includes a little-endian length, JSON payload, and IEEE 802.3 CRC-32; D1 calls `sync_all` before acknowledging every append.\n",
        results["generated_at_unix_seconds"],
        results["environment"]["os"].as_str().expect("OS string"),
        results["environment"]["arch"]
            .as_str()
            .expect("architecture string"),
        results["environment"]["build_profile"]
            .as_str()
            .expect("build profile string"),
        direct["iterations"],
        direct["p50_us"].as_f64().expect("p50"),
        direct["p95_us"].as_f64().expect("p95"),
        direct["p99_us"].as_f64().expect("p99"),
        journal["entries"],
        journal["elapsed_ms"].as_f64().expect("elapsed milliseconds"),
        journal["records_per_sec"].as_f64().expect("records per second"),
        journal["mb_per_sec"].as_f64().expect("MB per second"),
        journal["journal_bytes"]
    );
    fs::write(results_dir.join("microbenchmarks.md"), markdown).expect("write benchmark Markdown");

    let comparative_results = json!({
        "generated_at_unix_seconds": results["generated_at_unix_seconds"],
        "environment": results["environment"],
        "operation": comparative["operation"],
        "speedup_definition": comparative["speedup_definition"],
        "arms": comparative["arms"]
    });
    fs::write(
        results_dir.join("comparative_benchmarks.json"),
        serde_json::to_vec_pretty(&comparative_results)
            .expect("serialize comparative benchmark JSON"),
    )
    .expect("write comparative benchmark JSON");
    let comparative_markdown = format!(
        "# Castor D-02 Comparative Microbenchmark\n\n\
         Generated at Unix time `{}` on `{}` / `{}` using the `{}` profile. Each arm made 1,000 stop-and-wait `GetProjectionSummary` requests to the same physical `castord`; Arm B adds a persistent loopback HTTP/1.1 reverse proxy in front of the UDS backend.\n\n\
         | Arm | Transport | Iterations | p50 (µs) | p95 (µs) | p99 (µs) | Throughput (req/s) | Speedup factor |\n\
         |---|---|---:|---:|---:|---:|---:|---:|\n\
         | {} | {} | {} | {:.3} | {:.3} | {:.3} | {:.3} | {:.3}x |\n\
         | {} | {} | {} | {:.3} | {:.3} | {:.3} | {:.3} | {:.3}x |\n\n\
         Speedup is `HTTP proxy p50 / direct UDS p50`; HTTP is the 1.0x baseline.\n",
        results["generated_at_unix_seconds"],
        results["environment"]["os"].as_str().expect("OS string"),
        results["environment"]["arch"]
            .as_str()
            .expect("architecture string"),
        results["environment"]["build_profile"]
            .as_str()
            .expect("build profile string"),
        direct["arm"].as_str().expect("direct arm label"),
        direct["transport"].as_str().expect("direct transport"),
        direct["iterations"],
        direct["p50_us"].as_f64().expect("direct p50"),
        direct["p95_us"].as_f64().expect("direct p95"),
        direct["p99_us"].as_f64().expect("direct p99"),
        direct["throughput_requests_per_sec"]
            .as_f64()
            .expect("direct throughput"),
        direct["speedup_factor"]
            .as_f64()
            .expect("direct speedup"),
        http["arm"].as_str().expect("HTTP arm label"),
        http["transport"].as_str().expect("HTTP transport"),
        http["iterations"],
        http["p50_us"].as_f64().expect("HTTP p50"),
        http["p95_us"].as_f64().expect("HTTP p95"),
        http["p99_us"].as_f64().expect("HTTP p99"),
        http["throughput_requests_per_sec"]
            .as_f64()
            .expect("HTTP throughput"),
        http["speedup_factor"].as_f64().expect("HTTP speedup")
    );
    fs::write(
        results_dir.join("comparative_benchmarks.md"),
        comparative_markdown,
    )
    .expect("write comparative benchmark Markdown");
}

#[test]
#[ignore = "physical latency benchmark; run explicitly to refresh results artifacts"]
fn live_microbenchmarks() {
    let generated_at_unix_seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock after Unix epoch")
        .as_secs();
    let comparative = benchmark_comparative_rtt();
    let results = json!({
        "generated_at_unix_seconds": generated_at_unix_seconds,
        "environment": {
            "os": std::env::consts::OS,
            "arch": std::env::consts::ARCH,
            "build_profile": if cfg!(debug_assertions) { "debug" } else { "release" }
        },
        "d02_direct_uds_rtt": comparative["arms"][0],
        "comparative_uds_http_rtt": comparative,
        "crc32_journal_framing_and_storage": benchmark_crc32_journal()
    });
    write_results(&results);
    println!(
        "{}",
        serde_json::to_string_pretty(&results).expect("render benchmark summary")
    );
}
