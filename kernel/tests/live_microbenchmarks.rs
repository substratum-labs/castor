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
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const UDS_ITERATIONS: usize = 1_000;
const JOURNAL_ENTRIES: usize = 2_000;

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

fn benchmark_uds_rtt() -> Value {
    let root = tempfile::tempdir().expect("temporary castord benchmark root");
    let socket = root.path().join("castord.sock");
    let control_socket = root.path().join("control.sock");
    let _daemon = spawn_castord(root.path(), &socket, &control_socket);
    let mut client = GatewayClient::connect(&control_socket).expect("connect control socket");

    for request_id in 0..50 {
        let response = client
            .request(&projection_summary_request(request_id))
            .expect("warm-up AISA request");
        assert_eq!(response.status, "Ok");
    }

    let mut samples = Vec::with_capacity(UDS_ITERATIONS);
    for request_id in 0..UDS_ITERATIONS {
        let started = Instant::now();
        let response = client
            .request(&projection_summary_request(request_id))
            .expect("measured AISA request");
        let elapsed = started.elapsed();
        assert_eq!(response.status, "Ok");
        samples.push(elapsed);
    }
    samples.sort_unstable();
    let p50 = (duration_us(samples[499]) + duration_us(samples[500])) / 2.0;

    json!({
        "iterations": UDS_ITERATIONS,
        "operation": "GetProjectionSummary",
        "channel": "control",
        "p50_us": p50,
        "p95_us": duration_us(samples[949]),
        "p99_us": duration_us(samples[989])
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

    let uds = &results["d02_direct_uds_rtt"];
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
        uds["iterations"],
        uds["p50_us"].as_f64().expect("p50"),
        uds["p95_us"].as_f64().expect("p95"),
        uds["p99_us"].as_f64().expect("p99"),
        journal["entries"],
        journal["elapsed_ms"].as_f64().expect("elapsed milliseconds"),
        journal["records_per_sec"].as_f64().expect("records per second"),
        journal["mb_per_sec"].as_f64().expect("MB per second"),
        journal["journal_bytes"]
    );
    fs::write(results_dir.join("microbenchmarks.md"), markdown).expect("write benchmark Markdown");
}

#[test]
#[ignore = "physical latency benchmark; run explicitly to refresh results artifacts"]
fn live_microbenchmarks() {
    let generated_at_unix_seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock after Unix epoch")
        .as_secs();
    let results = json!({
        "generated_at_unix_seconds": generated_at_unix_seconds,
        "environment": {
            "os": std::env::consts::OS,
            "arch": std::env::consts::ARCH,
            "build_profile": if cfg!(debug_assertions) { "debug" } else { "release" }
        },
        "d02_direct_uds_rtt": benchmark_uds_rtt(),
        "crc32_journal_framing_and_storage": benchmark_crc32_journal()
    });
    write_results(&results);
    println!(
        "{}",
        serde_json::to_string_pretty(&results).expect("render benchmark summary")
    );
}
