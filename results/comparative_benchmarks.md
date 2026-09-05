# Castor D-02 Comparative Microbenchmark

Generated at Unix time `1788589611` on `macos` / `aarch64` using the `release` profile. Each arm made 1,000 stop-and-wait `GetProjectionSummary` requests to the same physical `castord`; Arm B adds a persistent loopback HTTP/1.1 reverse proxy in front of the UDS backend.

| Arm | Transport | Iterations | p50 (µs) | p95 (µs) | p99 (µs) | Throughput (req/s) | Speedup factor |
|---|---|---:|---:|---:|---:|---:|---:|
| Arm A | Direct UDS (control.sock) | 1000 | 12.875 | 17.584 | 26.250 | 73104.316 | 6.786x |
| Arm B | HTTP/1.1 loopback reverse proxy -> UDS | 1000 | 87.375 | 135.375 | 154.167 | 10506.768 | 1.000x |

Speedup is `HTTP proxy p50 / direct UDS p50`; HTTP is the 1.0x baseline.
