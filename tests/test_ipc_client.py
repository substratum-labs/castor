from __future__ import annotations

import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from collections.abc import Callable
from typing import Any

try:
    from castor.ipc_client import (
        MAX_FRAME_BYTES,
        AisaClient,
        AisaConnectionError,
        AisaCorrelationError,
        AisaFrameTooLargeError,
        AisaGatewayError,
        AisaProtocolError,
        AisaTimeoutError,
    )
except ModuleNotFoundError:
    from src.castor.ipc_client import (
        MAX_FRAME_BYTES,
        AisaClient,
        AisaConnectionError,
        AisaCorrelationError,
        AisaFrameTooLargeError,
        AisaGatewayError,
        AisaProtocolError,
        AisaTimeoutError,
    )


ServerHandler = Callable[[socket.socket], None]


def _receive_exact(current_socket: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while received < size:
        chunk = current_socket.recv(size - received)
        if not chunk:
            raise RuntimeError("client disconnected during test request")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def _receive_json_frame(current_socket: socket.socket) -> dict[str, Any]:
    header = _receive_exact(current_socket, 4)
    size = struct.unpack(">I", header)[0]
    decoded: Any = json.loads(_receive_exact(current_socket, size))
    if not isinstance(decoded, dict):
        raise AssertionError("request was not a JSON object")
    return decoded


def _json_frame(value: dict[str, Any]) -> bytes:
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _start_server(
    current_socket: socket.socket,
    handler: ServerHandler,
) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            handler(current_socket)
        except BaseException as exc:
            errors.append(exc)
        finally:
            current_socket.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


class AisaClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client_socket, self.server_socket = socket.socketpair()
        self.client = AisaClient("unused-test-socket")
        self.client._socket = self.client_socket

    def tearDown(self) -> None:
        self.client.close()
        self.server_socket.close()

    def finish_server(
        self,
        thread: threading.Thread,
        errors: list[BaseException],
    ) -> None:
        thread.join(1.0)
        self.assertFalse(thread.is_alive(), "test server did not finish")
        if errors:
            raise errors[0]

    def test_normal_request_and_response_framing(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            request = _receive_json_frame(current_socket)
            self.assertIsInstance(request["request_id"], str)
            self.assertEqual(request["op"], "echo")
            self.assertEqual(request["payload"], {"value": 7})
            response = {
                "request_id": request["request_id"],
                "status": "Ok",
                "payload": {"value": 7},
            }
            current_socket.sendall(_json_frame(response))

        thread, errors = _start_server(self.server_socket, handler)
        response = self.client.request("echo", {"value": 7})
        self.assertEqual(response["status"], "Ok")
        self.assertEqual(response["payload"], {"value": 7})
        self.finish_server(thread, errors)

    def test_fragmented_response_framing(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            request = _receive_json_frame(current_socket)
            frame = _json_frame(
                {
                    "request_id": request["request_id"],
                    "status": "Ok",
                    "fragmented": True,
                }
            )
            for byte in frame:
                current_socket.sendall(bytes((byte,)))

        thread, errors = _start_server(self.server_socket, handler)
        response = self.client.request("fragment")
        self.assertTrue(response["fragmented"])
        self.finish_server(thread, errors)

    def test_malformed_json_response(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            _receive_json_frame(current_socket)
            body = b"{not-json"
            current_socket.sendall(struct.pack(">I", len(body)) + body)

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaProtocolError):
            self.client.request("malformed")
        self.finish_server(thread, errors)

    def test_truncated_frame(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            _receive_json_frame(current_socket)
            current_socket.sendall(struct.pack(">I", 12) + b"short")

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaProtocolError):
            self.client.request("truncated")
        self.finish_server(thread, errors)

    def test_zero_length_frame(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            _receive_json_frame(current_socket)
            current_socket.sendall(struct.pack(">I", 0))

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaProtocolError):
            self.client.request("empty")
        self.finish_server(thread, errors)

    def test_oversized_frame(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            _receive_json_frame(current_socket)
            current_socket.sendall(struct.pack(">I", MAX_FRAME_BYTES + 1))

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaFrameTooLargeError) as raised:
            self.client.request("oversized")
        self.assertEqual(raised.exception.frame_size, MAX_FRAME_BYTES + 1)
        self.finish_server(thread, errors)

    def test_gateway_error_mapping(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            request = _receive_json_frame(current_socket)
            response = {
                "request_id": request["request_id"],
                "status": "Error",
                "error": {
                    "code": "PermissionDenied",
                    "message": "operation is not allowed",
                },
            }
            current_socket.sendall(_json_frame(response))

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaGatewayError) as raised:
            self.client.request("forbidden")
        self.assertEqual(raised.exception.code, "PermissionDenied")
        self.assertEqual(
            raised.exception.message,
            "operation is not allowed",
        )
        self.finish_server(thread, errors)

    def test_request_id_mismatch(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            _receive_json_frame(current_socket)
            response = {
                "request_id": "a-different-request",
                "status": "Ok",
            }
            current_socket.sendall(_json_frame(response))

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaCorrelationError) as raised:
            self.client.request("mismatch")
        self.assertEqual(raised.exception.actual, "a-different-request")
        self.finish_server(thread, errors)

    def test_socket_timeout(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            _receive_json_frame(current_socket)
            threading.Event().wait(0.1)

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaTimeoutError):
            self.client.request("slow", timeout=0.01)
        self.finish_server(thread, errors)

    def test_disconnection_before_response(self) -> None:
        def handler(current_socket: socket.socket) -> None:
            _receive_json_frame(current_socket)

        thread, errors = _start_server(self.server_socket, handler)
        with self.assertRaises(AisaConnectionError):
            self.client.request("disconnect")
        self.finish_server(thread, errors)


class AisaClientContextManagerTestCase(unittest.TestCase):
    def test_context_manager_connects_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = os.path.join(directory, "aisa.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(socket_path)
            listener.listen(1)

            def handler() -> None:
                connection, _ = listener.accept()
                with connection:
                    request = _receive_json_frame(connection)
                    response = {
                        "request_id": request["request_id"],
                        "status": "Ok",
                    }
                    connection.sendall(_json_frame(response))
                    self.assertEqual(connection.recv(1), b"")

            errors: list[BaseException] = []

            def guarded_handler() -> None:
                try:
                    handler()
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=guarded_handler, daemon=True)
            thread.start()
            client = AisaClient(socket_path, timeout=1.0)
            with client as connected_client:
                self.assertIs(connected_client, client)
                response = connected_client.request("context")
                self.assertEqual(response["status"], "Ok")
            self.assertIsNone(client._socket)

            thread.join(1.0)
            listener.close()
            self.assertFalse(thread.is_alive(), "context server did not finish")
            if errors:
                raise errors[0]


if __name__ == "__main__":
    unittest.main()
