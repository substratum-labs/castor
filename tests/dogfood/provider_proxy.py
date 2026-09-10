from __future__ import annotations

import argparse
import ipaddress
import json
import selectors
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_HOST = "api.openai.com"
ALLOWED_PORT = 443


class ProxyPolicyError(ValueError):
    """A proxy request falls outside the single-provider policy."""


def parse_allowed_connect_target(authority: str) -> tuple[str, int]:
    if (
        not authority
        or "@" in authority
        or "/" in authority
        or "?" in authority
        or "#" in authority
    ):
        raise ProxyPolicyError("CONNECT authority contains forbidden syntax")
    if authority.startswith("["):
        raise ProxyPolicyError("raw IP destinations are forbidden")
    host, separator, port_text = authority.rpartition(":")
    if not separator or not host or not port_text.isascii() or not port_text.isdigit():
        raise ProxyPolicyError(
            "CONNECT authority must contain an explicit numeric port"
        )
    host = host.lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ProxyPolicyError("raw IP destinations are forbidden")
    if host != ALLOWED_HOST or port_text != str(ALLOWED_PORT):
        raise ProxyPolicyError("CONNECT destination is not allowlisted")
    return host, ALLOWED_PORT


def validate_redirect_target(location: str) -> None:
    parsed = urlsplit(location)
    try:
        port = parsed.port
    except ValueError as error:
        raise ProxyPolicyError("redirect has an invalid port") from error
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.hostname.rstrip(".").lower() != ALLOWED_HOST
        or port not in (None, ALLOWED_PORT)
    ):
        raise ProxyPolicyError("redirect leaves the allowlisted provider origin")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    raise ProxyPolicyError("redirect uses a raw IP destination")


@dataclass
class ProxyAudit:
    request_count: int = 0
    client_to_provider_bytes: int = 0
    provider_to_client_bytes: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record_request(self) -> None:
        with self._lock:
            self.request_count += 1

    def record_bytes(
        self, *, client_to_provider: int = 0, provider_to_client: int = 0
    ) -> None:
        if client_to_provider < 0 or provider_to_client < 0:
            raise ValueError("byte counts cannot be negative")
        with self._lock:
            self.client_to_provider_bytes += client_to_provider
            self.provider_to_client_bytes += provider_to_client

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "request_count": self.request_count,
                "client_to_provider_bytes": self.client_to_provider_bytes,
                "provider_to_client_bytes": self.provider_to_client_bytes,
            }


class ProviderProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], audit: ProxyAudit) -> None:
        self.audit = audit
        super().__init__(server_address, ProviderProxyHandler)


class ProviderProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProviderProxyServer

    def do_CONNECT(self) -> None:  # noqa: N802
        try:
            host, port = parse_allowed_connect_target(self.path)
        except ProxyPolicyError:
            self.send_error(403, "CONNECT destination denied")
            return
        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except OSError:
            self.send_error(502, "provider connection failed")
            return
        self.server.audit.record_request()
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            self._relay(upstream)
        finally:
            upstream.close()
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._deny_non_connect()

    def do_POST(self) -> None:  # noqa: N802
        self._deny_non_connect()

    def do_PUT(self) -> None:  # noqa: N802
        self._deny_non_connect()

    def do_DELETE(self) -> None:  # noqa: N802
        self._deny_non_connect()

    def do_HEAD(self) -> None:  # noqa: N802
        self._deny_non_connect()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._deny_non_connect()

    def _deny_non_connect(self) -> None:
        self.send_error(405, "only CONNECT is permitted")

    def _relay(self, upstream: socket.socket) -> None:
        selector = selectors.DefaultSelector()
        selector.register(self.connection, selectors.EVENT_READ, (upstream, True))
        selector.register(upstream, selectors.EVENT_READ, (self.connection, False))
        try:
            while True:
                events = selector.select(timeout=30)
                if not events:
                    return
                for key, _ in events:
                    destination, outbound = key.data
                    data = key.fileobj.recv(64 * 1024)
                    if not data:
                        return
                    destination.sendall(data)
                    if outbound:
                        self.server.audit.record_bytes(client_to_provider=len(data))
                    else:
                        self.server.audit.record_bytes(provider_to_client=len(data))
        finally:
            selector.close()

    def log_message(self, format: str, *args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="api.openai.com-only CONNECT proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8080)
    parser.add_argument("--audit-path", type=Path, required=True)
    args = parser.parse_args(argv)
    audit = ProxyAudit()
    server = ProviderProxyServer((args.listen_host, args.listen_port), audit)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        args.audit_path.write_text(
            json.dumps(audit.snapshot(), sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
