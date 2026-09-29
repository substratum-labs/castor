#!/usr/bin/env python3
"""Physical Pi/FileBridge/HTTP-stub preflight. Never calls Ollama."""
import json
import multiprocessing
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_trusted_slot
from trusted_slot.common import write_json
from trusted_slot.fake_transport import bits_reply
from trusted_slot.real_model import HostExchange


def serve_stub(pipe, evidence):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/api/chat":
                self.send_error(404)
                return
            ledger = json.loads((evidence / "launcher/budget.json").read_bytes())
            assert ledger["reservations"][-1]["status"] == "RESERVED"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = bits_reply(payload, 5)
            calls = evidence / "stub-calls.jsonl"
            with calls.open("a") as out:
                out.write(json.dumps({"reservation_before_post": True, "path": self.path, "provider_calls": 0}) + "\n")
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class LoopbackServer(HTTPServer):
        def server_bind(self):
            # Fixed loopback stub needs no reverse DNS/network discovery.
            TCPServer.server_bind(self)
            self.server_name = "127.0.0.1"
            self.server_port = self.server_address[1]

    server = LoopbackServer(("127.0.0.1", 0), Handler)
    pipe.send(server.server_port)
    pipe.close()
    server.serve_forever()


def main():
    state = Path(sys.argv[sys.argv.index("--state-root") + 1])
    state.mkdir(parents=True, exist_ok=False)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    stub = context.Process(target=serve_stub, args=(child, state))
    stub.start()
    if not parent.poll(5):
        stub.terminate()
        stub.join()
        raise RuntimeError("stub startup timeout")
    endpoint = f"http://127.0.0.1:{parent.recv()}/api/chat"
    original_init = HostExchange.__init__
    original_transport = HostExchange.transport

    def init(exchange, *args, **kwargs):
        original_init(exchange, *args, **kwargs)
        assert exchange.fake
        exchange.transport = lambda payload, timeout: original_transport(payload, timeout, endpoint)

    sys.argv[0] = "run_trusted_slot.py"
    try:
        with patch.object(HostExchange, "__init__", init):
            code = run_trusted_slot.main()
        write_json(state / "http-stub-summary.json", {
            "transport": "REAL_LOOPBACK_HTTP_TO_TEST_STUB", "provider_calls": 0,
            "stub_posts": len((state / "stub-calls.jsonl").read_text().splitlines()) if (state / "stub-calls.jsonl").exists() else 0,
            "native_exit": code,
        })
        return code
    finally:
        stub.terminate()
        stub.join(timeout=2)
        if stub.is_alive():
            stub.kill()
            stub.join()


if __name__ == "__main__":
    raise SystemExit(main())
