"""Local Linux-UDS deterministic model fixture, using the native framed protocol."""

import hashlib
import json
import socket
import struct
import threading
from pathlib import Path

try:
    from .common import append_json, write_json
except ImportError:  # direct trusted controller invocation
    from common import append_json, write_json

MAX_FRAME = 16 * 1024 * 1024


def read_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        part = stream.recv(length - len(data))
        if not part:
            raise EOFError("truncated native frame")
        data.extend(part)
    return bytes(data)


def read_frame(stream):
    length = struct.unpack(">I", read_exact(stream, 4))[0]
    if not 1 <= length <= MAX_FRAME:
        raise ValueError("native frame too large")
    return json.loads(read_exact(stream, length))


def reply(request, mode, ordinal):
    if request["request"]["schema_version"] != 1 or mode not in ("fixing", "nonfixing"):
        raise ValueError("unsupported fixture request")
    if ordinal == 0:
        tool = {
            "type": "toolCall",
            "id": "read-1",
            "name": "castor_read_file",
            "arguments": {"path": "defect.txt"},
        }
    elif ordinal == 1:
        observations = [
            message
            for message in request["request"].get("messages", [])
            if message.get("role") == "toolResult"
            and message.get("toolCallId") == "read-1"
            and message.get("toolName") == "castor_read_file"
        ]
        if (
            len(observations) != 1
            or observations[0].get("isError") is not False
            or observations[0].get("content")
            != [{"type": "text", "text": "failing fixture\n"}]
        ):
            raise ValueError(
                "edit requires the successful native defect.txt read observation"
            )
        tool = {
            "type": "toolCall",
            "id": "edit-1",
            "name": "castor_edit_file",
            "arguments": {
                "path": "defect.txt",
                "edits": [
                    {
                        "oldText": "failing fixture",
                        "newText": "fixed fixture"
                        if mode == "fixing"
                        else "still failing fixture",
                    }
                ],
            },
        }
    else:
        # The pinned native extension synthesizes stop locally after terminal edit.
        raise ValueError("terminal edit unexpectedly requested another interaction")
    content = json.dumps(
        {
            "content": [tool],
            "stopReason": "toolUse",
            "usage": {"input": 12, "output": 8},
        },
        separators=(",", ":"),
    ).encode()
    return {
        "interaction_id": request["interaction_id"],
        "observation_region_id": f"region://observation/{ordinal}",
        "observation_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "content": list(content),
    }


class MockModel:
    def __init__(self, path, mode, evidence):
        self.path, self.mode, self.evidence = path, mode, Path(evidence)
        self.stop = threading.Event()
        self.calls = 0
        self.error = None
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(path)
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.summary()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def summary(self):
        write_json(
            self.evidence / "mock-summary.json",
            {
                "transport": "AF_UNIX",
                "socket": self.path,
                "mode": self.mode,
                "local_requests": self.calls,
                "provider_calls": 0,
                "error": self.error,
            },
        )

    def serve(self):
        try:
            while not self.stop.is_set():
                try:
                    stream, _ = self.listener.accept()
                except TimeoutError:
                    continue
                with stream:
                    stream.settimeout(5)
                    request = read_frame(stream)
                    ordinal = self.calls
                    self.calls += 1
                    append_json(
                        self.evidence / "mock-calls.jsonl",
                        {
                            "ordinal": ordinal,
                            "request": request,
                            "mode": self.mode,
                            "provider_calls": 0,
                        },
                    )
                    self.summary()
                    if self.mode == "timeout":
                        # A native interaction proves the real Pi child started.
                        self.stop.wait()
                        return
                    response = reply(request, self.mode, ordinal)
                    body = json.dumps(response, separators=(",", ":")).encode()
                    stream.sendall(struct.pack(">I", len(body)) + body)
                    append_json(
                        self.evidence / "mock-calls.jsonl",
                        {"ordinal": ordinal, "response": response},
                    )
        except Exception as error:
            self.error = str(error)
            self.summary()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)
        self.listener.close()
        self.summary()
