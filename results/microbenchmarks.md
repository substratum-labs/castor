# Castor Live Microbenchmarks

Generated at Unix time `1788589611` on `macos` / `aarch64` using the `release` profile.

## D-02 Direct UDS RTT

| Iterations | Operation | p50 (us) | p95 (us) | p99 (us) |
|---:|---|---:|---:|---:|
| 1000 | GetProjectionSummary | 12.875 | 17.584 | 26.250 |

## CRC-32 Journal Framing and D1 Storage

| Entries | Total (ms) | Records/s | MB/s | Journal bytes |
|---:|---:|---:|---:|---:|
| 2000 | 8308.051 | 240.730 | 0.153 | 1274679 |

Each journal record includes a little-endian length, JSON payload, and IEEE 802.3 CRC-32; D1 calls `sync_all` before acknowledging every append.
