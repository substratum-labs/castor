# Castor Client (`castor-client`)

Typed Python client for the **Castor OS AISA v0.1** Unix-domain socket IPC protocol.

## Features

- **Big-Endian 32-bit Framing**: Robust framing matching the Castor kernel wire protocol.
- **Exact Fragmented Reads**: Handles partial TCP/Unix socket chunks cleanly.
- **16 MiB Frame Guard**: Enforces frame size upper bounds to prevent memory exhaustion.
- **Request Correlation**: Thread-safe correlation of requests to responses via unique IDs.
- **Typed Error Hierarchy**: Clear exception mapping for gateway errors, connection errors, protocol errors, and timeouts.
- **Zero External Dependencies**: Implemented entirely with Python standard library.

## Usage

```python
from castor_client import AisaClient

with AisaClient("/run/castor/ipc.sock") as client:
    response = client.send_request("AdmitTurn", {"agent_id": "agent-1"})
    print(response)
```
