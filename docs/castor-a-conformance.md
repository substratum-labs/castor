# Castor A candidate: conformance and code tour

This page records the 2026-09-24 local candidate checks. It is not a release acceptance record. The supported A candidate is `castor-client` 0.7.0a1 talking to the Rust `castord` D1 host. The old `castor-kernel` 0.6 wheel remains a historical Python engine and is not part of this candidate.

## Authority path

`packages/castor-client/src/castor_client/session.py` limits Agent and operator requests before transport, opens a fresh connection per request, and surfaces transport or gateway failures without local fallback or automatic effect retry. `client.py` owns the framed AISA request and response correlation. `tests/dogfood/ring3_agent.py` is the Python guest; it uses `AgentSession` on the Roche-mounted Agent socket. The Rust host in `kernel/src/bin/castord.rs` enforces the physical channel split. The trusted actuator and evidence service use separate host channels.

## Invariant evidence

| Spec invariant | Local evidence | Remaining gate |
| --- | --- | --- |
| A-I1 Rust journal authority | `tests/test_cognitive_recovery_castord.py` exercises durable journal and projection across daemon restart; `tests/test_castor_a_physical.py` uses the same physical daemon helper for the Python guest. | Run the exact release candidate through a fresh-install end-to-end trace. |
| A-I2 Agent channel separation | `test_agent_channel_rejects_privileged_operations_without_append` compares journal and projection before and after forbidden Agent requests; positive control, actuator, and evidence paths run separately. | Roche container mount/identity evidence requires a working Docker runner. |
| A-I3 bound Action before effect | `test_control_actuator_and_evidence_channels_remain_usable` proves the prepared and armed attempt is acquired and settled; the existing cognitive recovery and dogfood tests cover binding and delivery order. | Run N1 against the exact candidate with independently observed target effects. |
| A-I4 conservative crash recovery | `test_python_guest_observes_unknown_after_arm_and_restart` observes `ArmedUnknown` and zero actuator effects; existing R9 and C1–C3 harnesses cover later crash points. | Run the full Roche C1–C3 physical suite on CI. |
| A-I5 fail-closed client | `test_python_guest_fails_closed_when_daemon_dies`, the client transport tests, and the malformed/cut-reply cases report errors without a Python fallback. | Exercise installed wheel in a clean environment against the daemon. |
| A-I6 single supported entrypoint | `test_client_wheel_has_no_python_authority_engine` inspects the built wheel and imports it under isolated Python; the documentation labels the root package and CLI/MCP as historical. | The old root package and `castor run` remain executable if explicitly installed. Migrating its consumers and retiring that distribution remain open. |

Local checks on this candidate: `uv run pytest -q` passed 632 tests with 9 skipped and 80 subtests; scoped Ruff check/format, Cargo fmt, Cargo clippy with warnings denied, and strict MkDocs build passed. The non-Docker Rust tests passed earlier on this branch. Docker was unavailable locally, so these results do not establish Roche container confinement or full N1/C1/C2/C3/D1/H1 conformance. No release, merge, or publication is implied.

The release gate must also decide how to retire the old `castor-kernel` distribution. Both Tiphys and castor-server depend on `castor-kernel>=0.5.1` and import its Python authority modules; a higher-version shim would satisfy those ranges and break them. The conservative candidate keeps `castor-kernel` frozen and distributes `castor-client` separately until those consumers migrate or pin an upper bound. The historical publish workflow is unchanged and must not publish this candidate as the root Python wheel.

The session façade currently validates response shape but returns dictionaries; operation-specific typed request and response objects from the design spec are still outstanding.
