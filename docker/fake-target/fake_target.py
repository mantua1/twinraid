"""Nachgebautes Unraid Groß für docker/ssh-test.sh: Ein/Aus, Wake-on-LAN und Syslog.

Nur für Tests. Die VMs bildet das nachgebaute virsh nach (Zustand in /state/vms.json).
Weiterer Zustand in /state:
  power-off       vorhanden: Groß ist „aus“ (Port 80 geschlossen)
  wol.log         empfangene Magic Packets mit der erwarteten MAC
  syslog.log      Meldungen an /dev/log, z. B. von guard.sh (Kennung twinraid-guard)
"""

import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path("/state")
MAC = bytes.fromhex(os.environ["FAKE_MAC"].replace(":", ""))
BOOT_SECONDS = 3.0


class Handler(BaseHTTPRequestHandler):
    """Die WebGUI: TwinRaid prüft nur, ob der Port offen ist."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def append(name: str, line: str) -> None:
    with (STATE / name).open("a") as handle:
        handle.write(line + "\n")


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


def syslog_loop() -> None:
    """Nimmt Meldungen von logger an (wie das Syslog von Unraid)."""
    Path("/dev/log").unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind("/dev/log")
    # Wie auf einem echten System darf jeder Prozess ins Syslog schreiben.
    Path("/dev/log").chmod(0o666)
    while True:
        data = sock.recv(8192)
        append("syslog.log", data.decode(errors="replace"))


threading.Thread(target=syslog_loop, daemon=True).start()
threading.Thread(target=wol_loop, daemon=True).start()
power_loop()
