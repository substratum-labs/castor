# T-337-E Pinned Duration Task Fixture

This directory contains the pinned task fixture for T-337-E physical execution:

## Contents

- `workspace_snapshot.tar.gz`: Single-defect Python repository archive (976 bytes).
  - SHA-256: `d64c1fb8f73530b81e43d621f2c9afad9ef2cc5c188c06ec5b0db7c389c4f99f`
  - Defect: In `duration.py`, `parse_duration('1d')` returns 3600 instead of 86400.
  - Tests: `tests/test_duration.py` (7 tests total; 1 failing before fix, 7 passing after fix).
- `workspace_snapshot.sha256`: Verified checksum file for `shasum -c workspace_snapshot.sha256`.
- `task_manifest.template.json`: Template task manifest pinning:
  - Task prompt and verification command
  - Strict resource limits: max 3 interactions, 512 tokens/call, 300s supervisor timeout, 1 action
  - Fresh idempotency key: `t337e-local-model-run-002`
  - Dynamic `carrier_base_image` placeholder to be populated with the post-fix carrier image ID (`substratum/castor-pi-carrier:v1@sha256:...`) built on the runner.
