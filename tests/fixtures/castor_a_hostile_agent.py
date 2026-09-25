"""Untrusted Roche guest probes that must remain outside its authority."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from castor_client import AisaClient, AisaGatewayError


def main() -> None:
    denied: list[str] = []
    for op in (
        "GrantCapability",
        "PresentSettlementCertificate",
        "AcquireDispatch",
    ):
        with AisaClient("/run/castor/ipc.sock", timeout=5) as client:
            try:
                client.request(op, {})
            except AisaGatewayError as error:
                denied.append(error.code)
            else:
                raise AssertionError(f"guest operation {op} was accepted")
    assert denied == ["UnauthorizedOpcode"] * 3

    for name in ("control.sock", "evidence.sock", "actuator.sock"):
        assert not (Path("/run/castor") / name).exists()
    with socket.socket() as stream:
        stream.settimeout(0.2)
        try:
            stream.connect(("198.51.100.1", 443))
        except OSError:
            pass
        else:
            raise AssertionError("raw network egress succeeded")
    print(json.dumps(denied), flush=True)


if __name__ == "__main__":
    main()
