from __future__ import annotations

import json
import os
import socket
import struct
import threading
import uuid
from types import TracebackType
from typing import Any

MAX_FRAME_BYTES = 16 * 1024 * 1024

__all__ = [
    "AisaClient",
    "AisaClientError",
    "AisaConnectionError",
    "AisaTimeoutError",
    "AisaProtocolError",
    "AisaFrameTooLargeError",
    "AisaCorrelationError",
    "AisaGatewayError",
]


class AisaClientError(Exception):
    """Base exception for AISA client failures."""


class AisaConnectionError(AisaClientError):
    """Raised when the Unix-domain socket cannot be used."""


class AisaTimeoutError(AisaClientError):
    """Raised when a socket operation times out."""


class AisaProtocolError(AisaClientError):
    """Raised when a peer sends an invalid AISA frame or response."""


class AisaFrameTooLargeError(AisaClientError):
    """Raised when an AISA frame exceeds the configured limit."""

    def __init__(self, frame_size: int) -> None:
        self.frame_size = frame_size
        self.maximum_size = MAX_FRAME_BYTES
        super().__init__(
            f"frame is {frame_size} bytes; maximum is {MAX_FRAME_BYTES} bytes"
        )


class AisaCorrelationError(AisaClientError):
    """Raised when a response belongs to a different request."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"response request_id {actual!r} does not match {expected!r}")


class AisaGatewayError(AisaClientError):
    """An error response returned by the AISA gateway."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AisaClient:
    """Synchronous client for the AISA v0.1 Unix-socket protocol."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        timeout: float | None = None,
    ) -> None:
        self._socket_path = os.fspath(socket_path)
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._lock = threading.RLock()

    def __enter__(self) -> AisaClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        """Connect to the configured Unix-domain socket if needed."""
        with self._lock:
            if self._socket is not None:
                return

            connected_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connected_socket.settimeout(self._timeout)
                connected_socket.connect(self._socket_path)
            except TimeoutError as exc:
                connected_socket.close()
                raise AisaTimeoutError(
                    f"connection to {self._socket_path!r} timed out"
                ) from exc
            except OSError as exc:
                connected_socket.close()
                raise AisaConnectionError(
                    f"could not connect to {self._socket_path!r}: {exc}"
                ) from exc
            self._socket = connected_socket

    def close(self) -> None:
        """Close the current socket. Calling this method repeatedly is safe."""
        with self._lock:
            current_socket = self._socket
            self._socket = None
            if current_socket is not None:
                current_socket.close()

    def request(
        self,
        op: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one request and return its correlated response object."""
        if not isinstance(op, str):
            raise AisaProtocolError("op must be a string")
        if payload is not None and not isinstance(payload, dict):
            raise AisaProtocolError("payload must be a dictionary")

        request_id = str(uuid.uuid4())
        request_object: dict[str, Any] = {
            "request_id": request_id,
            "op": op,
            "payload": {} if payload is None else payload,
        }
        request_body = self._encode_request(request_object)

        with self._lock:
            self.connect()
            current_socket = self._require_socket()
            previous_timeout = current_socket.gettimeout()
            effective_timeout = self._timeout if timeout is None else timeout
            try:
                current_socket.settimeout(effective_timeout)
                self._send_frame(current_socket, request_body)
                response_body = self._receive_frame(current_socket)
                return self._decode_response(response_body, request_id)
            except TimeoutError as exc:
                self._discard_socket(current_socket)
                raise AisaTimeoutError("AISA request timed out") from exc
            except OSError as exc:
                self._discard_socket(current_socket)
                raise AisaConnectionError(
                    f"AISA socket operation failed: {exc}"
                ) from exc
            except (
                AisaConnectionError,
                AisaFrameTooLargeError,
                AisaProtocolError,
            ):
                self._discard_socket(current_socket)
                raise
            finally:
                if self._socket is current_socket:
                    try:
                        current_socket.settimeout(previous_timeout)
                    except OSError:
                        self._discard_socket(current_socket)

    @staticmethod
    def _encode_request(request_object: dict[str, Any]) -> bytes:
        try:
            body = json.dumps(
                request_object,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AisaProtocolError(f"request is not JSON serializable: {exc}") from exc
        if len(body) > MAX_FRAME_BYTES:
            raise AisaFrameTooLargeError(len(body))
        return body

    @staticmethod
    def _send_frame(current_socket: socket.socket, body: bytes) -> None:
        if len(body) > MAX_FRAME_BYTES:
            raise AisaFrameTooLargeError(len(body))
        header = struct.pack(">I", len(body))
        current_socket.sendall(header + body)

    def _receive_frame(self, current_socket: socket.socket) -> bytes:
        header = self._receive_exact(current_socket, 4, "frame header")
        frame_size = struct.unpack(">I", header)[0]
        if frame_size == 0:
            raise AisaProtocolError("zero-length frames are not valid")
        if frame_size > MAX_FRAME_BYTES:
            raise AisaFrameTooLargeError(frame_size)
        return self._receive_exact(current_socket, frame_size, "frame payload")

    @staticmethod
    def _receive_exact(
        current_socket: socket.socket,
        size: int,
        description: str,
    ) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while received < size:
            chunk = current_socket.recv(size - received)
            if not chunk:
                if received == 0 and description == "frame header":
                    raise AisaConnectionError(
                        "peer disconnected before sending a response"
                    )
                raise AisaProtocolError(
                    f"truncated {description}: expected {size} bytes, "
                    f"received {received}"
                )
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_response(body: bytes, expected_id: str) -> dict[str, Any]:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AisaProtocolError("response is not valid UTF-8") from exc
        try:
            decoded: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AisaProtocolError("response contains malformed JSON") from exc
        if not isinstance(decoded, dict):
            raise AisaProtocolError("response JSON must be an object")

        response: dict[str, Any] = decoded
        actual_id = response.get("request_id")
        if not isinstance(actual_id, str):
            raise AisaProtocolError("response request_id must be a string")
        if actual_id != expected_id:
            raise AisaCorrelationError(expected_id, actual_id)

        status = response.get("status")
        if status == "Error":
            error = response.get("error")
            if not isinstance(error, dict):
                raise AisaProtocolError("gateway error must be an object")
            code = error.get("code")
            message = error.get("message")
            if not isinstance(code, str):
                raise AisaProtocolError("gateway error code must be a string")
            if not isinstance(message, str):
                raise AisaProtocolError("gateway error message must be a string")
            raise AisaGatewayError(code, message)
        if status != "Ok":
            raise AisaProtocolError("response status must be either 'Ok' or 'Error'")
        return response

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise AisaConnectionError("AISA client is not connected")
        return self._socket

    def _discard_socket(self, current_socket: socket.socket) -> None:
        if self._socket is current_socket:
            self._socket = None
        current_socket.close()
