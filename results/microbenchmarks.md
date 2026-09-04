# Castor Live Microbenchmarks

Generated at Unix time `1788547037` on `macos` / `aarch64` using the `release` profile.

## D-02 Direct UDS RTT

| Iterations | Operation | p50 (us) | p95 (us) | p99 (us) |
|---:|---|---:|---:|---:|
| 1000 | GetProjectionSummary | 10.083 | 24.292 | 44.375 |

## CRC-32 Journal Framing and D1 Storage

| Entries | Total (ms) | Records/s | MB/s | Journal bytes |
|---:|---:|---:|---:|---:|
| 2000 | 9040.993 | 221.215 | 0.141 | 1274679 |

Each journal record includes a little-endian length, JSON payload, and IEEE 802.3 CRC-32; D1 calls `sync_all` before acknowledging every append.
