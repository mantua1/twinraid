"""Lokaler Ersatz für die Unraid-API auf 127.0.0.1 (HTTP oder HTTPS), nur für Tests."""

import json
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path

from tests.support.tls import install_test_runtime
from twinraid.shared.addresses import configured
from twinraid.shared.tls import fingerprint


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def graphql_reply(domains: list[dict[str, str]]) -> Reply:
    return Reply(body=json.dumps({"data": {"vms": {"domains": domains}}}).encode())


def graphql_error(code: str | None, message: str) -> Reply:
    error: dict[str, object] = {"message": message}
    if code is not None:
        error["extensions"] = {"code": code}
    return Reply(body=json.dumps({"errors": [error], "data": None}).encode())


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        # Absichtlich kaputte Verbindungen (z. B. TLS gegen HTTP) nicht ins Testlog schreiben.
        return


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


# Server-ID der Unraid-API; sie steht vor der UUID in jeder VM-ID (PrefixedID).
SERVER_ID = "c0ffee42"
READ = "READ_ANY"
UPDATE = "UPDATE_ANY"


@dataclass
class FakeVm:
    uuid: str
    name: str
    state: str = "SHUTOFF"

    @property
    def id(self) -> str:
        return f"{SERVER_ID}:{self.uuid}"


class FakeVmHost:
    """Unraid-API mit VMs: Abfrage, vm.start und vm.forceStop, Rechte je API-Key (VMS:…)."""

    def __init__(self, vms: list[FakeVm], keys: dict[str, set[str]]) -> None:
        self.vms = vms
        self.keys = keys
        self.mutations: list[tuple[str, str]] = []

    def __call__(self, request: Received) -> Reply:
        permissions = self.keys.get(request.headers.get("x-api-key", ""))
        if permissions is None:
            return graphql_error("UNAUTHENTICATED", "API key validation failed")
        document = json.loads(request.body)
        query = str(document["query"])
        if query.startswith("query"):
            if READ not in permissions:
                return graphql_error("FORBIDDEN", "Forbidden resource")
            return graphql_reply(
                [{"id": vm.id, "name": vm.name, "state": vm.state} for vm in self.vms]
            )
        field_name = "forceStop" if "forceStop" in query else "start"
        if UPDATE not in permissions:
            return graphql_error("FORBIDDEN", "Forbidden resource")
        uuid = str(document["variables"]["id"]).split(":")[-1]
        vm = next((vm for vm in self.vms if vm.uuid == uuid), None)
        if vm is None:
            return graphql_error(None, "Failed to set VM state: Domain not found")
        self.mutations.append((field_name, uuid))
        if field_name == "start":
            if vm.state != "SHUTOFF":
                return graphql_error(None, "Failed to set VM state: Invalid state transition")
            vm.state = "RUNNING"
        else:
            vm.state = "SHUTOFF"
        return Reply(body=json.dumps({"data": {"vm": {field_name: True}}}).encode())


class FakeUnraid:
    """Antwortet auf POST /graphql mit einer festgelegten Antwort und merkt sich jede Anfrage."""

    def __init__(self, tmp_path: Path, tls: bool = False) -> None:
        self.reply: Reply | Callable[[], Reply] = graphql_reply([])
        # Wenn gesetzt, entscheidet er anhand der Anfrage (z. B. FakeVmHost).
        self.responder: Callable[[Received], Reply] | None = None
        self.received: list[Received] = []
        self.certificate_fingerprint: str | None = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                received = Received(
                    self.path, {k.lower(): v for k, v in self.headers.items()}, body
                )
                fake.received.append(received)
                if fake.responder is not None:
                    reply = fake.responder(received)
                else:
                    reply = fake.reply() if callable(fake.reply) else fake.reply
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        if tls:
            runtime = tmp_path / "fake-unraid-tls"
            leaf, _ca = install_test_runtime(runtime, configured([ip_address("127.0.0.1")]))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(runtime / "cert.pem", runtime / "key.pem")
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.certificate_fingerprint = fingerprint(leaf)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def other_certificate_fingerprint(tmp_path: Path) -> str:
    leaf, _ = install_test_runtime(tmp_path / "other-tls", configured([ip_address("127.0.0.1")]))
    return fingerprint(leaf)
