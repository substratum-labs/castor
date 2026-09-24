# Castor A: Rust authority with a Python Agent

Castor A's bounded single-node D1 path consists of a Rust `castord` host, the Roche sandbox carrier, a thin AISA client, and host-side trusted adapters. `castord` owns capability checks, journal transitions, effect admission, and recovery. A Python Agent may request those transitions over the guest socket; it cannot make them authoritative by itself.

## Install the candidate client

From a source checkout:

```bash
uv build --no-sources packages/castor-client
python -m pip install packages/castor-client/dist/castor_client-0.7.0a1-py3-none-any.whl
```

The client wheel has no runtime dependencies and contains neither the old Python gate nor its journal or runner. It does not start `castord`. The host must provision a storage root, separate sockets, a Roche guest, and any required trusted actuator/evidence service before admitting a Turn.

## Agent and operator boundary

```python
from castor_client import AgentSession, OperatorSession

# This path exists only inside the Roche guest.
agent = AgentSession("/run/castor/ipc.sock")

# A host operator uses a separate control socket outside that guest.
operator = OperatorSession("/run/castor/control.sock")
```

These objects are transport conveniences. `AgentSession.request(op, payload)` accepts only Agent-channel operations and returns the correlated AISA response envelope; `OperatorSession` accepts only management operations. Rust `castord` applies the final channel allowlist and authority checks. The operator socket, evidence socket, actuator socket, durable storage, and target workspace are never mounted into the Agent guest. Each request uses a fresh Unix-socket connection with a five-second default timeout; an uncertain request is never automatically retried as a new action.

The complete bounded Agent request sequence is exercised in `tests/dogfood/ring3_agent.py`: admit a current Turn, request and consume an Interaction, durably publish Action payloads, commit the Turn, register Actions, present admission, and record dispatch. The trusted actuator separately acquires attempt-bound payload and submits settlement evidence. A Python tool callable inside the Agent is not a trusted actuator.

## Scope and migration

`castor-kernel` 0.6 and `from castor import Castor` remain a historical Python prototype for existing consumers. They do not use the Rust D1 authority path. Castor A's supported client is the separate `castor-client` package; upgrading to it is an explicit API migration. Existing Tiphys and castor-server code still imports the old package and must be migrated under separate reviewed tasks before that package can be removed from their installations.

Castor A does not promise arbitrary third-party exactly-once I/O, distributed ownership, Python-frame restoration, or parity for the prototype's MMU, fork, speculative replay, and general budget APIs. `castor run agent.py` source packaging and a Python-free default Agent are later product work.
