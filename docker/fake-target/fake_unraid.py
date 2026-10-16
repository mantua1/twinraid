"""Nachgebautes Unraid Groß für docker/ssh-test.sh: GraphQL-API, Ein/Aus, Wake-on-LAN.

Nur für Tests. Zustand in /state:
  api-keys.json   {"<Key>": ["READ_ANY", "UPDATE_ANY"]}
  vm-state        RUNNING oder SHUTOFF
  power-off       vorhanden: Groß ist „aus“ (Port 80 geschlossen)
  mutations.log   ausgeführte VM-Befehle der API
  wol.log         empfangene Magic Packets mit der erwarteten MAC
  other-vm        vorhanden: eine zweite VM läuft (sonst ist sie aus)
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path("/state")
SERVER_ID = "f00dcafe"
VM_UUID = os.environ["FAKE_VM_UUID"]
VM_NAME = os.environ["FAKE_VM_NAME"]
OTHER_VM_UUID = "7c6b5a49-3827-4161-9504-f3e2d1c0b9a8"
OTHER_VM_NAME = "Second VM"
MAC = bytes.fromhex(os.environ["FAKE_MAC"].replace(":", ""))
BOOT_SECONDS = 3.0


def read(name: str, default: str) -> str:
    try:
        return (STATE / name).read_text().strip()
    except FileNotFoundError:
        return default


def append(name: str, line: str) -> None:
    with (STATE / name).open("a") as handle:
        handle.write(line + "\n")


def error(code: str | None, message: str) -> dict[str, object]:
    entry: dict[str, object] = {"message": message}
    if code is not None:
        entry["extensions"] = {"code": code}
    return {"errors": [entry], "data": None}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        keys = json.loads(read("api-keys.json", "{}"))
        permissions = keys.get(self.headers.get("x-api-key", ""))
        self.reply(answer(body, permissions))

    def reply(self, document: dict[str, object]) -> None:
        data = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def answer(body: dict[str, object], permissions: list[str] | None) -> dict[str, object]:
    if permissions is None:
        return error("UNAUTHENTICATED", "API key validation failed")
    query = str(body.get("query", ""))
    if query.startswith("query"):
        if "READ_ANY" not in permissions:
            return error("FORBIDDEN", "Forbidden resource")
        domain = {"id": f"{SERVER_ID}:{VM_UUID}", "name": VM_NAME, "state": read("vm-state", "SHUTOFF")}
        other_state = "RUNNING" if (STATE / "other-vm").exists() else "SHUTOFF"
        other = {"id": f"{SERVER_ID}:{OTHER_VM_UUID}", "name": OTHER_VM_NAME, "state": other_state}
        return {"data": {"vms": {"domains": [domain, other]}}}
    if "UPDATE_ANY" not in permissions:
        return error("FORBIDDEN", "Forbidden resource")
    field = "forceStop" if "forceStop" in query else "start"
    variables = body.get("variables")
    uuid = str(variables.get("id", "") if isinstance(variables, dict) else "").split(":")[-1]
    if uuid != VM_UUID:
        return error(None, "Failed to set VM state: Domain not found")
    if field == "start" and read("vm-state", "SHUTOFF") != "SHUTOFF":
        return error(None, "Failed to set VM state: Invalid state transition")
    (STATE / "vm-state").write_text("RUNNING\n" if field == "start" else "SHUTOFF\n")
    append("mutations.log", f"{field} {uuid}")
    return {"data": {"vm": {field: True}}}


def power_loop() -> None:
    """HTTP auf Port 80, solange /state/power-off fehlt."""
    server: ThreadingHTTPServer | None = None
    while True:
        off = (STATE / "power-off").exists()
        if off and server is not None:
            server.shutdown()
            server.server_close()
            server = None
        if not off and server is None:
            server = ThreadingHTTPServer(("0.0.0.0", 80), Handler)  # noqa: S104
            threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.2)


def boot() -> None:
    (STATE / "power-off").unlink(missing_ok=True)


def wol_loop() -> None:
    """Empfängt Magic Packets (UDP 9) und „bootet“, wenn Groß aus ist."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 9))
    while True:
        data, address = sock.recvfrom(1024)
        if data == b"\xff" * 6 + MAC * 16:
            append("wol.log", f"{MAC.hex(':')} from {address[0]}")
            if (STATE / "power-off").exists():
                threading.Timer(BOOT_SECONDS, boot).start()


threading.Thread(target=wol_loop, daemon=True).start()
power_loop()
