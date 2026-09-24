from __future__ import annotations

import unittest
from pathlib import Path

try:
    from castor_client import (
        AgentSession,
        AisaConnectionError,
        AisaProtocolError,
        OperatorSession,
    )
except ModuleNotFoundError:
    from src.castor_client import (
        AgentSession,
        AisaConnectionError,
        AisaProtocolError,
        OperatorSession,
    )


class SessionContract(unittest.TestCase):
    def test_agent_rejects_control_operation_before_connecting(self) -> None:
        session = AgentSession(Path("/nonexistent/agent.sock"))
        with self.assertRaises(ValueError):
            session.request("GrantCapability", {})

    def test_operator_rejects_agent_operation_before_connecting(self) -> None:
        session = OperatorSession(Path("/nonexistent/control.sock"))
        with self.assertRaises(ValueError):
            session.request("AdmitTurn", {})

    def test_allowed_agent_operation_fails_closed_when_daemon_is_absent(self) -> None:
        session = AgentSession(Path("/nonexistent/agent.sock"))
        with self.assertRaises(AisaConnectionError):
            session.request("AdmitTurn", {"agent_id": "a"})

    def test_session_reports_malformed_success_outcome(self) -> None:
        class MalformedTransport:
            def request(self, op: str, payload: dict[str, object]) -> dict[str, object]:
                return {"request_id": "x", "status": "Ok", "outcome": "not-an-object"}

        session = AgentSession(MalformedTransport())
        with self.assertRaises(AisaProtocolError):
            session.request("AdmitTurn", {"agent_id": "a"})


if __name__ == "__main__":
    unittest.main()
