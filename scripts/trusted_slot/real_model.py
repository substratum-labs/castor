"""Trusted-host Ollama exchange; never imported by Pi or the verifier."""

import hashlib
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

try:
    from .common import write_bytes, write_json
    from .mock_model import read_frame
except ImportError:  # direct trusted controller invocation
    from common import write_bytes, write_json
    from mock_model import read_frame

MODEL = "qwen3.5:9b"
MODEL_DIGEST = "6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7"
OLLAMA_VERSION = "0.34.1"
MAX_CALLS = 3


class HTTPTransportError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        message = "malformed Ollama HTTP body" if status == 200 else f"Ollama HTTP {status}"
        super().__init__(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def validate_model_pin():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    request = urllib.request.Request("http://127.0.0.1:11434/api/tags", method="GET")
    with opener.open(request, timeout=5) as response:
        data = json.loads(response.read(1024 * 1024 + 1))
    version_request = urllib.request.Request("http://127.0.0.1:11434/api/version", method="GET")
    with opener.open(version_request, timeout=5) as response:
        version = json.loads(response.read(1024 * 1024 + 1)).get("version")
    matches = [item for item in data.get("models", []) if item.get("name") == MODEL]
    if (len(matches) != 1 or matches[0].get("digest") != MODEL_DIGEST
            or version != OLLAMA_VERSION):
        raise ValueError("local Ollama model digest or runtime version mismatch")
    return {"name": MODEL, "digest": MODEL_DIGEST, "version": version}


def model_request(native):
    request = native.get("request", {})
    interaction = native.get("interaction_id")
    if (
        request.get("schema_version") != 1
        or not isinstance(interaction, str)
        or request.get("interaction_id") != interaction
        or not isinstance(request.get("messages"), list)
        or not request["messages"]
        or not isinstance(request.get("tools"), list)
    ):
        raise ValueError("invalid native model request")
    canonical = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    if native.get("request_digest") != "sha256:" + hashlib.sha256(canonical).hexdigest():
        raise ValueError("native request digest mismatch")
    requested = request.get("parameters", {}).get("max_tokens", 512)
    if type(requested) is not int or requested <= 0:
        raise ValueError("invalid requested token budget")
    messages = []
    first_system = True
    for message in request["messages"]:
        role, content = message.get("role"), message.get("content", "")
        if role == "system":
            body = (
                content if isinstance(content, str)
                else "\n".join(block.get("text", "") for block in content
                               if block.get("type") == "text")
                if isinstance(content, list) else ""
            )
            sections = message.get("sections") or {}
            if first_system:
                first_system = False
                parts = [body] + [value for value in sections.values() if isinstance(value, str)]
            else:
                parts = [body] + [
                    f'Updated system prompt section "{name}":\n\n{value}'
                    if value is not None else f'Removed system prompt section "{name}".'
                    for name, value in sections.items()
                ]
            messages.append({"role": "system", "content": "\n\n".join(p for p in parts if p)})
        elif role in ("user", "assistant"):
            body = (
                content if isinstance(content, str)
                else "\n".join(block.get("text", "") for block in content
                               if block.get("type") == "text")
                if isinstance(content, list) else ""
            )
            converted = {"role": role, "content": body}
            if role == "assistant":
                calls = [
                    {"id": block["id"], "type": "function", "function": {
                        "name": block["name"], "arguments": block.get("arguments") or {}}}
                    for block in content if block.get("type") == "toolCall"
                ] if isinstance(content, list) else []
                if not calls and isinstance(message.get("tool_calls"), list):
                    calls = message["tool_calls"]
                if calls:
                    converted["tool_calls"] = calls
            messages.append(converted)
        elif role in ("toolResult", "tool"):
            body = (
                content if isinstance(content, str)
                else "\n".join(
                    block.get("text") if isinstance(block.get("text"), str)
                    else json.dumps(block) for block in content
                ) if isinstance(content, list) else json.dumps(content)
            )
            converted = {"role": "tool", "content": body}
            if message.get("toolCallId") or message.get("tool_call_id"):
                converted["tool_call_id"] = str(message.get("toolCallId") or message.get("tool_call_id"))
            if message.get("toolName") or message.get("tool_name") or message.get("name"):
                converted["tool_name"] = str(message.get("toolName") or message.get("tool_name") or message.get("name"))
            messages.append(converted)
        else:
            raise ValueError("unsupported native message role")
    tools = [
        tool if tool.get("type") == "function" and isinstance(tool.get("function"), dict)
        else {"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {}}}
        for tool in request["tools"]
    ]
    return {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.2, "seed": 17, "num_ctx": 32768,
            "num_predict": min(requested, 512),
        },
        "messages": messages,
        **({"tools": tools} if tools else {}),
    }


def native_response(native, raw):
    message = raw.get("message")
    if not isinstance(message, dict):
        raise ValueError("missing model message")
    blocks = []
    if isinstance(message.get("content"), str) and message["content"].strip():
        blocks.append({"type": "text", "text": message["content"]})
    for ordinal, call in enumerate(message.get("tool_calls") or [], 1):
        function = call.get("function", call)
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        name = function.get("name")
        allowed = {
            tool.get("name") or tool.get("function", {}).get("name")
            for tool in native["request"]["tools"]
        }
        if name not in allowed or not isinstance(arguments, dict):
            raise ValueError("malformed or unoffered model tool call")
        blocks.append({
            "type": "toolCall",
            "id": call.get("id") or f"call-{native['interaction_id']}-{ordinal}",
            "name": name, "arguments": arguments,
        })
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    input_count, output_count = raw.get("prompt_eval_count"), raw.get("eval_count")
    if type(input_count) is not int or type(output_count) is not int:
        raise ValueError("missing model usage")
    if output_count > 512 or min(input_count, output_count) < 0:
        raise ValueError("model output usage outside budget")
    stop_reason = (
        "toolUse" if any(block["type"] == "toolCall" for block in blocks)
        else "length" if raw.get("done_reason") == "length" else "stop"
    )
    body = json.dumps({
        "content": blocks, "stopReason": stop_reason,
        "usage": {"input": input_count, "output": output_count},
    }, separators=(",", ":")).encode()
    return {
        "interaction_id": native["interaction_id"],
        "observation_region_id": "region://model-observation/" + native["interaction_id"],
        "observation_digest": "sha256:" + hashlib.sha256(body).hexdigest(),
        "content": list(body),
    }


class HostExchange:
    def __init__(self, evidence, deadline_seconds):
        self.evidence = Path(evidence)
        self.bridge = self.evidence / "bridge"
        self.bridge.mkdir(parents=True, exist_ok=True)
        self.deadline = time.monotonic() + deadline_seconds
        self.lock = threading.Lock()
        self.ledger = {"model": MODEL, "digest": MODEL_DIGEST, "max_calls": MAX_CALLS, "reservations": []}
        write_json(self.evidence / "budget.json", self.ledger)

    @staticmethod
    def transport(payload, timeout):
        data = json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
            with opener.open(request, timeout=timeout) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("Ollama response too large")
                try:
                    parsed = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise HTTPTransportError(response.status, body) from error
                return parsed, body, response.status
        except urllib.error.HTTPError as error:
            raise HTTPTransportError(error.code, error.read(4 * 1024 * 1024 + 1)) from error

    def poll(self):
        with self.lock:
            for path in sorted(self.bridge.glob("request-*.json")):
                reservation_started = None
                nonce = path.stem.removeprefix("request-")
                answer = self.bridge / ("response-" + nonce + ".json")
                error = self.bridge / ("error-" + nonce + ".json")
                if answer.exists() or error.exists():
                    continue
                try:
                    native = json.loads(path.read_bytes())
                    key = native["interaction_id"]
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    previous = next((r for r in self.ledger["reservations"] if r["interaction_id"] == key), None)
                    if previous:
                        if previous["request_sha256"] != digest or previous["status"] != "COMPLETED":
                            raise ValueError("duplicate interaction changed or incomplete")
                        write_json(answer, json.loads((self.bridge / previous["response_file"]).read_bytes()))
                        continue
                    if len(self.ledger["reservations"]) >= MAX_CALLS:
                        raise ValueError("unique HTTP call budget exhausted")
                    payload = model_request(native)
                    ordinal = len(self.ledger["reservations"]) + 1
                    reservation_started = time.monotonic()
                    record = {"ordinal": ordinal, "interaction_id": key, "request_sha256": digest, "status": "RESERVED", "reserved_unix_ns": time.time_ns(), "input_tokens": None, "output_tokens": None, "measurement": "INCOMPLETE", "response_file": answer.name}
                    self.ledger["reservations"].append(record)
                    write_json(self.evidence / "budget.json", self.ledger)  # durable before HTTP
                    write_json(self.bridge / f"http-request-{ordinal}.json", payload)
                    write_bytes(self.bridge / f"http-request-{ordinal}.raw", json.dumps(payload, separators=(",", ":")).encode())
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("task deadline expired before HTTP")
                    delivered = self.transport(payload, min(remaining, 300))  # exactly one invocation
                    raw, raw_body, http_status = delivered if isinstance(delivered, tuple) else (delivered, json.dumps(delivered).encode(), 200)
                    write_bytes(self.bridge / f"http-response-{ordinal}.raw", raw_body)
                    record["http_status"] = http_status
                    write_json(self.bridge / f"http-response-{ordinal}.json", raw)
                    record["input_tokens"] = raw.get("prompt_eval_count") if type(raw.get("prompt_eval_count")) is int else None
                    record["output_tokens"] = raw.get("eval_count") if type(raw.get("eval_count")) is int else None
                    for field in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
                        value = raw.get(field)
                        record[field + "_ns"] = value if type(value) is int else None
                    if record["input_tokens"] is not None and record["output_tokens"] is not None:
                        record["measurement"] = "COMPLETE"
                    response = native_response(native, raw)
                    write_json(answer, response)
                    record["status"] = "COMPLETED"
                    record["elapsed_seconds"] = time.monotonic() - reservation_started
                    write_json(self.evidence / "budget.json", self.ledger)
                except Exception as exc:
                    if 'record' in locals() and record in self.ledger["reservations"] and record["status"] == "RESERVED":
                        record["status"] = "FAILED"
                        record["error"] = type(exc).__name__ + ": " + str(exc)
                        if isinstance(exc, HTTPTransportError):
                            record["http_status"] = exc.status
                            write_bytes(self.bridge / f"http-response-{record['ordinal']}.raw", exc.body)
                        record["elapsed_seconds"] = time.monotonic() - reservation_started
                        write_json(self.evidence / "budget.json", self.ledger)
                    write_json(error, {"error": type(exc).__name__ + ": " + str(exc)})
                finally:
                    if 'record' in locals():
                        del record


class FileBridgeModel:
    """Same-kernel controller UDS server; durable files cross the Engine mount."""

    def __init__(self, socket_path, evidence):
        self.socket_path = socket_path
        self.bridge = Path(evidence) / "bridge"
        self.bridge.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(socket_path)
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                stream, _ = self.listener.accept()
            except TimeoutError:
                continue
            with stream:
                try:
                    stream.settimeout(300)
                    native = read_frame(stream)
                    nonce = uuid.uuid4().hex
                    write_json(self.bridge / ("request-" + nonce + ".json"), native)
                    answer = self.bridge / ("response-" + nonce + ".json")
                    error = self.bridge / ("error-" + nonce + ".json")
                    while not self.stop.is_set() and not answer.exists() and not error.exists():
                        time.sleep(0.05)
                    if answer.exists():
                        body = answer.read_bytes().strip()
                        stream.sendall(struct.pack(">I", len(body)) + body)
                except (OSError, ValueError, EOFError):
                    pass

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
        self.listener.close()
