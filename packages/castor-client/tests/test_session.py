from __future__ import annotations

import unittest
from pathlib import Path

try:
    from castor_client import (
        AdmitTurn,
        AgentRequest,
        AgentSession,
        AisaConnectionError,
        AisaProtocolError,
        AisaResponse,
        GrantCapability,
        OperatorRequest,
        OperatorSession,
        RequestInteraction,
    )
except ModuleNotFoundError:
    from src.castor_client import (
        AdmitTurn,
        AgentRequest,
        AgentSession,
        AisaConnectionError,
        AisaProtocolError,
        AisaResponse,
        GrantCapability,
        OperatorRequest,
        OperatorSession,
        RequestInteraction,
    )


class SessionContract(unittest.TestCase):
    class RecordingTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def request(self, op: str, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append((op, payload))
            return {
                "request_id": "contract",
                "status": "Ok",
                "outcome": {"type": "Admitted"},
            }

    def test_agent_rejects_control_operation_before_connecting(self) -> None:
        transport = self.RecordingTransport()
        session = AgentSession(transport)
        with self.assertRaises(ValueError):
            session.request("GrantCapability", {})
        self.assertEqual(transport.calls, [])

    def test_operator_rejects_agent_operation_before_connecting(self) -> None:
        transport = self.RecordingTransport()
        session = OperatorSession(transport)
        with self.assertRaises(ValueError):
            session.request("AdmitTurn", {})
        self.assertEqual(transport.calls, [])

    def test_agent_sends_one_allowed_request_with_original_payload(self) -> None:
        transport = self.RecordingTransport()
        session = AgentSession(transport)
        payload = {"agent_id": "a", "turn_id": 3}
        response = session.request("AdmitTurn", payload)
        self.assertEqual(response["status"], "Ok")
        self.assertEqual(response["outcome"], {"type": "Admitted"})
        self.assertEqual(transport.calls, [("AdmitTurn", payload)])

    def test_typed_agent_request_returns_response_object(self) -> None:
        transport = self.RecordingTransport()
        payload = {"agent_id": "a"}
        response = AgentSession(transport).send(AgentRequest("AdmitTurn", payload))
        self.assertIsInstance(response, AisaResponse)
        self.assertEqual(response.request_id, "contract")
        self.assertEqual(response.outcome_type, "Admitted")
        self.assertEqual(response.outcome, {"type": "Admitted"})
        self.assertEqual(transport.calls, [("AdmitTurn", payload)])

    def test_operation_specific_requests_preserve_role_and_wire_payload(self) -> None:
        transport = self.RecordingTransport()
        admit = AdmitTurn(
            agent_id="agent-a",
            turn_id=3,
            lease_epoch=0,
            base_projection_digest="sha256:base",
            cap_id="cap-a",
        )
        response = AgentSession(transport).send(admit)
        self.assertEqual(response.outcome_type, "Admitted")
        self.assertEqual(
            transport.calls,
            [
                (
                    "AdmitTurn",
                    {
                        "agent_id": "agent-a",
                        "turn_id": 3,
                        "lease_epoch": 0,
                        "base_projection_digest": "sha256:base",
                        "cap_id": "cap-a",
                    },
                )
            ],
        )
        with self.assertRaises(TypeError):
            AgentSession(transport).send(GrantCapability(grant={}))
        self.assertEqual(len(transport.calls), 1)

    def test_interaction_without_query_descriptor_omits_optional_wire_key(self) -> None:
        transport = self.RecordingTransport()
        AgentSession(transport).send(
            RequestInteraction(
                interaction_id="i-1", lease_epoch=0, request_digest="sha256:r"
            )
        )
        self.assertEqual(
            transport.calls[0],
            (
                "RequestInteraction",
                {
                    "interaction_id": "i-1",
                    "lease_epoch": 0,
                    "request_digest": "sha256:r",
                },
            ),
        )

    def test_typed_requests_cannot_cross_roles(self) -> None:
        transport = self.RecordingTransport()
        with self.assertRaises(TypeError):
            AgentSession(transport).send(OperatorRequest("GrantCapability", {}))
        with self.assertRaises(TypeError):
            OperatorSession(transport).send(AgentRequest("AdmitTurn", {}))
        self.assertEqual(transport.calls, [])

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

    def test_session_rejects_non_ok_transport_response(self) -> None:
        class NonOkTransport:
            def request(self, op: str, payload: dict[str, object]) -> dict[str, object]:
                return {
                    "request_id": "x",
                    "status": "Error",
                    "outcome": {"type": "Admitted"},
                }

        with self.assertRaises(AisaProtocolError):
            AgentSession(NonOkTransport()).request("AdmitTurn", {})

    def test_session_rejects_outcome_without_a_type(self) -> None:
        class MissingTypeTransport:
            def request(self, op: str, payload: dict[str, object]) -> dict[str, object]:
                return {"request_id": "x", "status": "Ok", "outcome": {}}

        session = AgentSession(MissingTypeTransport())
        with self.assertRaises(AisaProtocolError):
            session.request("AdmitTurn", {"agent_id": "a"})

    def test_operator_accepts_untyped_journal_read_result(self) -> None:
        class JournalTransport:
            def request(self, op: str, payload: dict[str, object]) -> dict[str, object]:
                return {"request_id": "x", "status": "Ok", "outcome": {"entries": []}}

        response = OperatorSession(JournalTransport()).request("InspectJournal", {})
        self.assertEqual(response["outcome"], {"entries": []})


if __name__ == "__main__":
    unittest.main()
