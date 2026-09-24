from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.dogfood.ring3_agent import (
    MAX_FRAME_BYTES,
    AgentConfig,
    AgentPhase,
    AisaClient,
    ProtocolError,
    Ring3Agent,
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def recv_exact(stream: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = stream.recv(size - len(result))
        if not part:
            raise AssertionError("peer closed a partial frame")
        result.extend(part)
    return bytes(result)


class FakeAisaServer:
    def __init__(self, path: Path, handler) -> None:
        self.path = path
        self.handler = handler
        self.requests: list[dict[str, object]] = []
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=2)

    def _serve(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(self.path))
                listener.listen()
                self.ready.set()
                while True:
                    stream, _ = listener.accept()
                    with stream:
                        length_bytes = recv_exact(stream, 4)
                        length = struct.unpack(">I", length_bytes)[0]
                        request = json.loads(recv_exact(stream, length))
                        if request == {}:
                            return
                        self.requests.append(request)
                        response = self.handler(request)
                        if response is None:
                            return
                        raw = canonical(response)
                        stream.sendall(struct.pack(">I", len(raw)) + raw)
        except BaseException as error:  # captured for the test thread
            self.error = error
            self.ready.set()

    def close(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(str(self.path))
                stream.sendall(struct.pack(">I", 2) + b"{}")
        except OSError:
            pass
        self.thread.join(timeout=2)
        if self.error is not None:
            raise self.error


class Ring3AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.socket_path = self.root / "agent.sock"

    def test_client_uses_big_endian_framing_and_correlates_request_id(self) -> None:
        def handler(request):
            return {
                "request_id": request["request_id"],
                "status": "Ok",
                "outcome": {"type": "Admitted"},
            }

        server = FakeAisaServer(self.socket_path, handler)
        self.addCleanup(server.close)
        response = AisaClient(self.socket_path).request("AdmitTurn", {"lease_epoch": 0})

        self.assertEqual(response["outcome"]["type"], "Admitted")
        self.assertEqual(server.requests[0]["op"], "AdmitTurn")
        self.assertEqual(server.requests[0]["payload"], {"lease_epoch": 0})

    def test_client_rejects_oversized_request_before_connecting(self) -> None:
        client = AisaClient(self.socket_path)
        with self.assertRaisesRegex(ProtocolError, "16 MiB"):
            client.request("EnsureRegion", {"content": "x" * MAX_FRAME_BYTES})

    def test_client_rejects_oversized_response_without_allocating_payload(self) -> None:
        def serve_oversized() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(self.socket_path))
                listener.listen(1)
                ready.set()
                stream, _ = listener.accept()
                with stream:
                    size = struct.unpack(">I", recv_exact(stream, 4))[0]
                    recv_exact(stream, size)
                    stream.sendall(struct.pack(">I", MAX_FRAME_BYTES + 1))

        ready = threading.Event()
        thread = threading.Thread(target=serve_oversized, daemon=True)
        thread.start()
        ready.wait(timeout=2)
        with self.assertRaisesRegex(ProtocolError, "invalid AISA frame length"):
            AisaClient(self.socket_path).request("AdmitTurn", {})
        thread.join(timeout=2)

    def test_client_rejects_response_for_another_request(self) -> None:
        server = FakeAisaServer(
            self.socket_path,
            lambda _request: {
                "request_id": "substituted",
                "status": "Ok",
                "outcome": {"type": "Admitted"},
            },
        )
        self.addCleanup(server.close)
        with self.assertRaisesRegex(ProtocolError, "request_id"):
            AisaClient(self.socket_path).request("AdmitTurn", {})

    def test_two_phase_publication_binds_every_action_before_registration(self) -> None:
        completion = {
            "schema_version": 1,
            "files": [
                {
                    "path": "src/castor/ipc_client.py",
                    "content_utf8": "typed client\n",
                },
                {
                    "path": "tests/test_ipc_client.py",
                    "content_utf8": "typed tests\n",
                },
            ],
            "explanation": "bounded change",
        }
        completion_bytes = canonical(completion)
        consume_count = 0
        admit_count = 0

        def handler(request):
            nonlocal admit_count, consume_count
            op = request["op"]
            if op == "AdmitTurn":
                admit_count += 1
                outcome = {"type": "Admitted"}
                if admit_count == 2:
                    outcome["unsettled_effects_snapshot"] = {
                        "author": "Core",
                        "turn_id": 8,
                        "region_ref": "observation:unsettled_effects:8",
                        "attempts": [],
                    }
            elif op == "ConsumeInteraction":
                consume_count += 1
                if consume_count == 1:
                    outcome = {"type": "RejectedStaleAuthority"}
                else:
                    outcome = {
                        "type": "InteractionConsumed",
                        "payload": {
                            "interaction_id": "model-7",
                            "observation_region_id": "region://model/result-7",
                            "observation_digest": digest(completion_bytes),
                            "content": list(completion_bytes),
                            "lease_epoch": 1,
                        },
                    }
            elif op == "PresentAdmissionCertificate":
                outcome = {
                    "type": "AttemptArmed",
                    "attempt_id": 100
                    + len([r for r in server.requests if r["op"] == op]),
                }
            else:
                outcomes = {
                    "AdmitTurn": "Admitted",
                    "RequestInteraction": "InteractionRequested",
                    "EnsureRegion": "Success",
                    "CommitTurn": "TurnCommitted",
                    "RegisterAction": "ActionRegistered",
                    "RecordDispatchAttempt": "DispatchRecorded",
                }
                outcome = {"type": outcomes[op]}
            return {
                "request_id": request["request_id"],
                "status": "Ok",
                "outcome": outcome,
            }

        server = FakeAisaServer(self.socket_path, handler)
        self.addCleanup(server.close)
        config = AgentConfig(
            socket_path=self.socket_path,
            agent_id="agent:dogfood-coding-agent",
            turn_id=7,
            lease_epoch=0,
            consume_lease_epoch=1,
            base_projection_digest=digest(b"base"),
            capability_id="cap-dogfood",
            generation=3,
            interaction_id="model-7",
            request_digest=digest(b"prompt"),
        )

        agent = Ring3Agent(config)
        result = agent.run()

        self.assertEqual(agent.phase, AgentPhase.OBSERVE_SETTLEMENTS)
        self.assertEqual(len(result.actions), 8)
        operations = [request["op"] for request in server.requests]
        self.assertEqual(
            operations[0:4],
            [
                "AdmitTurn",
                "RequestInteraction",
                "ConsumeInteraction",
                "ConsumeInteraction",
            ],
        )
        commit_index = operations.index("CommitTurn")
        self.assertTrue(all(op == "EnsureRegion" for op in operations[4:commit_index]))
        self.assertTrue(
            all(
                op == "RegisterAction"
                for op in operations[commit_index + 1 : commit_index + 9]
            )
        )
        admit = server.requests[0]["payload"]
        self.assertEqual(admit["lease_epoch"], 0)
        commit = server.requests[commit_index]["payload"]
        self.assertEqual(commit["lease_epoch"], 1)
        self.assertEqual(
            {binding["action_id"] for binding in commit["action_bindings"]},
            set(commit["action_manifest"]),
        )
        manifest_request = next(
            request
            for request in server.requests
            if request["op"] == "EnsureRegion"
            and request["payload"]["content_digest"] == commit["action_manifest_digest"]
        )
        self.assertEqual(
            bytes(manifest_request["payload"]["content"]),
            ("\n".join(commit["action_manifest"]) + "\n").encode("utf-8"),
        )
        self.assertTrue(
            all(
                binding["actuator_id"] == "repo-workspace-actuator"
                for binding in commit["action_bindings"]
            )
        )
        agent.observe_settlements(8, digest(b"settled projection"))
        self.assertEqual(agent.phase, AgentPhase.COMPLETE)
        self.assertEqual(
            [request["op"] for request in server.requests[-4:]],
            ["AdmitTurn", "EnsureRegion", "EnsureRegion", "CommitTurn"],
        )
        observation_admission = server.requests[-4]
        self.assertEqual(observation_admission["op"], "AdmitTurn")
        self.assertEqual(observation_admission["payload"]["turn_id"], 8)
        self.assertEqual(observation_admission["payload"]["lease_epoch"], 0)
        observation_commit = server.requests[-1]["payload"]
        self.assertEqual(observation_commit["lease_epoch"], 0)
        self.assertEqual(observation_commit["action_manifest"], [])
        self.assertEqual(observation_commit["action_bindings"], [])

    def test_environment_config_exposes_only_the_agent_socket(self) -> None:
        environment = {
            "CASTOR_IPC_SOCKET": "/run/castor/ipc.sock",
            "CASTOR_AGENT_ID": "agent:dogfood-coding-agent",
            "CASTOR_TURN_ID": "4",
            "CASTOR_BASE_PROJECTION_DIGEST": digest(b"projection"),
            "CASTOR_CAPABILITY_ID": "cap-dogfood",
            "CASTOR_GENERATION": "2",
            "CASTOR_INTERACTION_ID": "interaction-4",
            "CASTOR_REQUEST_DIGEST": digest(b"prompt"),
            "CASTOR_CONTROL_SOCKET": "/run/castor/control.sock",
            "CASTOR_EVIDENCE_SOCKET": "/run/castor/evidence.sock",
            "CASTOR_ACTUATOR_SOCKET": "/run/castor/actuator.sock",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            config = AgentConfig.from_env()

        self.assertEqual(config.socket_path, Path("/run/castor/ipc.sock"))
        self.assertEqual(config.lease_epoch, 0)
        self.assertFalse(
            any(
                name in AgentConfig.environment_keys()
                for name in (
                    "CASTOR_CONTROL_SOCKET",
                    "CASTOR_EVIDENCE_SOCKET",
                    "CASTOR_ACTUATOR_SOCKET",
                )
            )
        )

    def test_agent_revalidates_closed_model_completion_and_utf8_limit(self) -> None:
        config = AgentConfig(
            socket_path=self.socket_path,
            agent_id="agent:dogfood-coding-agent",
            turn_id=7,
            lease_epoch=0,
            consume_lease_epoch=1,
            base_projection_digest=digest(b"base"),
            capability_id="cap-dogfood",
            generation=3,
            interaction_id="model-7",
            request_digest=digest(b"prompt"),
        )
        valid = {
            "schema_version": 1,
            "files": [
                {
                    "path": "src/castor/ipc_client.py",
                    "content_utf8": "client\n",
                },
                {
                    "path": "tests/test_ipc_client.py",
                    "content_utf8": "tests\n",
                },
            ],
            "explanation": "bounded",
        }
        extra = {**valid, "shell": "rm -rf ."}
        oversized = json.loads(json.dumps(valid))
        oversized["files"][0]["content_utf8"] = "é" * 32_769

        for completion in (extra, oversized):
            with self.subTest(completion=completion), self.assertRaises(ProtocolError):
                Ring3Agent(config)._build_actions(completion)

    def test_action_ids_are_unique_across_turns(self) -> None:
        completion = {
            "schema_version": 1,
            "files": [
                {"path": "src/castor/ipc_client.py", "content_utf8": "client\n"},
                {"path": "tests/test_ipc_client.py", "content_utf8": "tests\n"},
            ],
            "explanation": "bounded",
        }

        def config(turn_id: int) -> AgentConfig:
            return AgentConfig(
                socket_path=self.socket_path,
                agent_id="agent:dogfood-coding-agent",
                turn_id=turn_id,
                lease_epoch=0,
                consume_lease_epoch=1,
                base_projection_digest=digest(b"base"),
                capability_id="cap-dogfood",
                generation=3,
                interaction_id=f"model-{turn_id}",
                request_digest=digest(b"prompt"),
            )

        first = Ring3Agent(config(7))._build_actions(completion)
        second = Ring3Agent(config(8))._build_actions(completion)
        self.assertTrue(
            {action.action_id for action in first}.isdisjoint(
                action.action_id for action in second
            )
        )


if __name__ == "__main__":
    unittest.main()
