# Castor Client (`castor-client`)

Typed Python client for the **Castor OS AISA v0.1** Unix-domain socket IPC protocol. The Rust `castord` process owns authorization, journal transitions, recovery, and effect admission. This package only sends requests and validates responses; it has no local kernel fallback.

## Features

- **Big-Endian 32-bit Framing**: Robust framing matching the Castor kernel wire protocol.
- **Exact Fragmented Reads**: Handles partial TCP/Unix socket chunks cleanly.
- **16 MiB Frame Guard**: Enforces frame size upper bounds to prevent memory exhaustion.
- **Request Correlation**: Thread-safe correlation of requests to responses via unique IDs.
- **Typed Error Hierarchy**: Clear exception mapping for gateway errors, connection errors, protocol errors, and timeouts.
- **Zero External Dependencies**: Implemented entirely with Python standard library.

## Usage

```python
from castor_client import AgentRequest, AgentSession

def admit_turn(admission_payload: dict[str, object]) -> dict[str, object]:
    agent = AgentSession("/run/castor/ipc.sock")
    return agent.send(AgentRequest("AdmitTurn", admission_payload)).outcome
```

`admission_payload` must contain the current Turn, lease, projection digest, and capability reference supplied by the host. See the physical `tests/dogfood/ring3_agent.py` journey for the bounded D1 request sequence. The Agent session accepts only Agent-channel operations. Host operators use `OperatorSession` with a separate `control.sock`; neither client class can grant authority by itself. Each request opens a fresh connection with a five-second socket idle timeout, not a total request deadline, and never retries an uncertain action automatically.

`AisaClient` remains available for protocol-level integrations. It does not make the old in-process Python `Castor()` facade equivalent to Rust `castord`.

Operation-specific request objects are available for all current Agent and operator D1 opcodes. For example, `AgentSession.send(AdmitTurn(...))` sends a payload with named fields and returns an `AisaResponse[AdmitTurnOutcome]` for static type checkers. `RequestInteraction` omits its optional descriptor when absent, as required by the Rust parser. The original `AgentRequest` and `OperatorRequest` envelopes and low-level `request(op, payload)` remain available for protocol-level callers. Outcome types describe the Rust wire shapes; callers still inspect `outcome["type"]` to distinguish admission from a governed rejection. Only `castord` validates authority and D1 preconditions.
