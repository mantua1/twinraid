"""SSH zu Unraid Groß mit den Keys „status“ und „power“ (Konzept 3.5 und 5.6).

Auf Groß erzwingt authorized_keys den Aufruf von guard.sh im passenden Modus; der
hier gesendete Text landet dort nur in SSH_ORIGINAL_COMMAND und wird mit festen
Texten verglichen. Parameter gibt es nicht: Welche VM gemeint ist, steht nur in
power.conf auf Groß.

Externe Programme laufen ausschließlich mit Argumentlisten, nie über eine Shell
(CLAUDE.md, Regel 3). Die Ausgabe von ssh wird nur einer Ursache aus einer
festen Liste zugeordnet und nie geloggt.
"""

import asyncio
import base64
import contextlib
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from twinraid.config import TwinRaidConfig
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.addresses import IpAddress
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.files import write_file_atomic

SSH_BINARY = Path("/usr/bin/ssh")
SSH_KEYSCAN_BINARY = Path("/usr/bin/ssh-keyscan")
CONNECT_TIMEOUT_SECONDS = 5
# Zeitlimits je Anfrage: die Grenze von guard.sh für virsh (Konzept 3.5) plus 15 s.
COMMAND_TIMEOUT_SECONDS = 30.0
VM_START_TIMEOUT_SECONDS = 105.0
VM_FORCE_STOP_TIMEOUT_SECONDS = 75.0
VM_SHUTDOWN_TIMEOUT_SECONDS = 45.0
KEYSCAN_TIMEOUT_SECONDS = 15.0
REMOTE_USER = "root"
KEY_TYPE = "ssh-ed25519"
GUARD_PATH = "/boot/config/twinraid/guard.sh"
# TwinRaid 0.4.3 braucht guard.sh Version 2 (Modus status, Befehle ohne Parameter).
REQUIRED_GUARD_VERSION = 2
# guard.sh antwortet auf "check" mit "twinraid-guard <Version> <Modus> ok".
GUARD_CHECK_PATTERN = re.compile(r"twinraid-guard (\d+) (status|power) ok", re.ASCII)
# Exit-Codes von guard.sh: 2 abgelehnt, 3 erlaubt, aber von einer Prüfung abgewiesen.
# ssh selbst meldet Fehler mit 255.
GUARD_DENIED_EXIT = 2
GUARD_REFUSED_EXIT = 3
SSH_ERROR_EXIT = 255
_REFUSAL_PATTERN = re.compile(r"twinraid-guard: refused ([a-z-]+)", re.ASCII)
_REFUSALS: dict[str, ActionOutcome] = {
    "vm-not-configured": ActionOutcome.VM_NOT_CONFIGURED,
    "vm-running": ActionOutcome.VM_RUNNING,
    "other-vms-running": ActionOutcome.OTHER_VMS_RUNNING,
    "vm-state-unknown": ActionOutcome.VM_SERVICE_UNAVAILABLE,
    # Auf Groß laufen schon zwei vm-watch-Schleifen; eine alte endet bald.
    "watch-busy": ActionOutcome.WATCH_INTERRUPTED,
}
_MAX_OUTPUT_BYTES = 64 * 1024
# Feste Meldungen von OpenSSH (LC_ALL=C) und ihre Bedeutung.
_HOST_KEY_MESSAGES = ("Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED")
_AUTH_MESSAGES = ("Permission denied",)
_UNREACHABLE_MESSAGES = (
    "Connection refused",
    "timed out",
    "No route to host",
    "Network is unreachable",
    "Could not resolve",
)
_CLOSED_MESSAGES = ("closed by remote host", "Connection closed")
# Feste Umgebung: englische Meldungen für classify(), keine geerbten Variablen.
SSH_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


class GuardKey(StrEnum):
    """Die SSH-Keys von TwinRaid; jeder erzwingt auf Groß den gleichnamigen Modus von guard.sh."""

    # Nur check und vm-watch, verändert nichts (ab v0.4.3).
    STATUS = "status"
    # VM starten, herunterfahren, erzwingen und Groß ausschalten.
    POWER = "power"

    @property
    def comment(self) -> str:
        return f"twinraid-{self.value}"

    @property
    def guard_command(self) -> str:
        return f"/bin/bash {GUARD_PATH} {self.value}"


def key_files(paths: DataPaths, key: GuardKey) -> tuple[Path, Path]:
    """Privater und öffentlicher Teil eines Keys in /data/secrets."""
    if key is GuardKey.STATUS:
        return paths.ssh_status_key, paths.ssh_status_public_key
    return paths.ssh_power_key, paths.ssh_power_public_key


class SshError(Exception):
    """Der SSH-Aufruf ist gescheitert; outcome sagt, warum."""

    def __init__(
        self, outcome: ActionOutcome, detail: str, connection_closed: bool = False
    ) -> None:
        super().__init__(f"{outcome.value}: {detail}")
        self.outcome = outcome
        self.detail = detail
        # Groß hat die Verbindung beendet, z. B. weil es gerade herunterfährt.
        self.connection_closed = connection_closed


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    key: Path
    known_hosts: Path

    @classmethod
    def of(cls, config: TwinRaidConfig, paths: DataPaths, key: GuardKey) -> SshTarget:
        target = config.servers.target
        private, _public = key_files(paths, key)
        return cls(str(target.host), target.ssh_port, private, paths.target_known_hosts)

    def missing(self) -> SshError | None:
        """Fehlt der Key oder der bestätigte Hostkey, wird keine Verbindung versucht."""
        if not self.key.is_file():
            return SshError(ActionOutcome.SSH_KEY_MISSING, "no SSH key")
        if not self.known_hosts.is_file():
            return SshError(ActionOutcome.SSH_HOST_UNTRUSTED, "host key not confirmed")
        return None


def ssh_arguments(
    target: SshTarget, request: Sequence[str], binary: Path = SSH_BINARY
) -> list[str]:
    """Feste Argumentliste: keine Konfigurationsdateien, nur dieser Key, strikte Hostkey-Prüfung."""
    options = {
        "BatchMode": "yes",
        "IdentitiesOnly": "yes",
        "IdentityAgent": "none",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "StrictHostKeyChecking": "yes",
        "UserKnownHostsFile": str(target.known_hosts),
        "GlobalKnownHostsFile": "/dev/null",
        "HostKeyAlgorithms": KEY_TYPE,
        "UpdateHostKeys": "no",
        "ConnectTimeout": str(CONNECT_TIMEOUT_SECONDS),
        "ServerAliveInterval": "5",
        "ServerAliveCountMax": "2",
        "LogLevel": "ERROR",
    }
    arguments = [
        str(binary),
        "-F",
        "/dev/null",
        "-T",
        "-i",
        str(target.key),
        "-p",
        str(target.port),
    ]
    for name, value in options.items():
        arguments += ["-o", f"{name}={value}"]
    return [*arguments, "--", f"{REMOTE_USER}@{target.host}", *request]


def classify(returncode: int, stderr: str) -> SshError | None:
    """Ordnet das Ergebnis eines ssh-Aufrufs einer Ursache zu; None heißt Erfolg."""
    if returncode == 0:
        return None
    if returncode == GUARD_DENIED_EXIT:
        return SshError(ActionOutcome.SSH_DENIED, "guard.sh rejected the request")
    if returncode == GUARD_REFUSED_EXIT:
        match = _REFUSAL_PATTERN.search(stderr)
        outcome = _REFUSALS.get(match.group(1)) if match is not None else None
        if outcome is not None:
            return SshError(outcome, "guard.sh refused the request")
        return SshError(ActionOutcome.REMOTE_FAILED, "remote exit code 3")
    if returncode != SSH_ERROR_EXIT:
        return SshError(ActionOutcome.REMOTE_FAILED, f"remote exit code {returncode}")
    if any(message in stderr for message in _HOST_KEY_MESSAGES):
        return SshError(ActionOutcome.SSH_HOST_KEY_MISMATCH, "host key verification failed")
    if any(message in stderr for message in _AUTH_MESSAGES):
        return SshError(ActionOutcome.SSH_AUTH_FAILED, "the key was rejected")
    if any(message in stderr for message in _UNREACHABLE_MESSAGES):
        return SshError(ActionOutcome.SSH_UNREACHABLE, "SSH is not reachable")
    closed = any(message in stderr for message in _CLOSED_MESSAGES)
    return SshError(ActionOutcome.SSH_FAILED, "ssh failed", connection_closed=closed)


async def _run(arguments: list[str], time_limit: float) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=SSH_ENVIRONMENT,
    )
    try:
        async with asyncio.timeout(time_limit):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    # Nach communicate() ist der Exit-Code immer gesetzt.
    returncode = process.returncode if process.returncode is not None else SSH_ERROR_EXIT
    return (
        returncode,
        stdout[:_MAX_OUTPUT_BYTES].decode(errors="replace"),
        stderr[:_MAX_OUTPUT_BYTES].decode(errors="replace"),
    )


class GuardClient:
    """Einzelne Anfragen an guard.sh; jede ist eine eigene Anmeldung mit genau einem Key."""

    def __init__(
        self,
        target: SshTarget,
        binary: Path = SSH_BINARY,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self.target = target
        self._binary = binary
        self._timeout = timeout

    async def check(self) -> str:
        """Verbindungstest ohne Wirkung; gibt die erste Zeile der Antwort zurück."""
        stdout = await self._request("check", self._timeout)
        return stdout.strip().splitlines()[0] if stdout.strip() else ""

    async def _request(self, request: str, time_limit: float) -> str:
        missing = self.target.missing()
        if missing is not None:
            raise missing
        arguments = ssh_arguments(self.target, [request], self._binary)
        try:
            returncode, stdout, stderr = await _run(arguments, time_limit)
        except TimeoutError:
            raise SshError(ActionOutcome.SSH_UNREACHABLE, "ssh timed out") from None
        except OSError:
            raise SshError(ActionOutcome.SSH_FAILED, "ssh could not be started") from None
        error = classify(returncode, stderr)
        if error is not None:
            raise error
        return stdout


class SshPower(GuardClient):
    """Die Aktionen im Modus "power"; die VM bestimmt power.conf auf Groß."""

    async def vm_start(self) -> None:
        await self._request("vm-start", max(self._timeout, VM_START_TIMEOUT_SECONDS))

    async def vm_shutdown(self) -> None:
        """ACPI-Signal an die VM (virsh shutdown); erzwingt nichts (E8)."""
        await self._request("vm-shutdown", max(self._timeout, VM_SHUTDOWN_TIMEOUT_SECONDS))

    async def vm_force_stop(self) -> None:
        await self._request("vm-force-stop", max(self._timeout, VM_FORCE_STOP_TIMEOUT_SECONDS))

    async def poweroff(self) -> None:
        """guard.sh schaltet Groß nur aus, wenn keine VM läuft (Konzept 3.5)."""
        await self._request("poweroff", self._timeout)


# --- Hostkey und Schlüssel (für twinraid-admin) -------------------------------------


@dataclass(frozen=True)
class HostKey:
    """ed25519-Hostkey von Groß: Zeile für known_hosts und SHA-256-Fingerabdruck."""

    known_hosts_line: str
    fingerprint: str


def key_fingerprint(key_base64: str) -> str:
    """Wie ssh-keygen -l: SHA256:<Base64 ohne Auffüllung>."""
    blob = base64.b64decode(key_base64, validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def _key_blob_type(key_base64: str) -> str | None:
    try:
        blob = base64.b64decode(key_base64, validate=True)
    except ValueError:
        return None
    if len(blob) < 4:
        return None
    length = int.from_bytes(blob[:4], "big")
    return blob[4 : 4 + length].decode(errors="replace")


def known_hosts_host(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def parse_host_key(output: str, host: str, port: int) -> HostKey:
    """Liest die ed25519-Zeile aus der Ausgabe von ssh-keyscan. Wirft ValueError."""
    keys = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == KEY_TYPE and _key_blob_type(parts[2]) == KEY_TYPE:
            keys.add(parts[2])
    if len(keys) != 1:
        raise ValueError("expected exactly one ed25519 host key")
    key = keys.pop()
    line = f"{known_hosts_host(host, port)} {KEY_TYPE} {key}"
    return HostKey(line, key_fingerprint(key))


async def scan_host_key(
    host: str,
    port: int,
    binary: Path = SSH_KEYSCAN_BINARY,
    time_limit: float = KEYSCAN_TIMEOUT_SECONDS,
) -> HostKey:
    """Holt den Hostkey ohne Anmeldung. Er muss danach vom Admin verglichen werden."""
    arguments = [str(binary), "-T", str(CONNECT_TIMEOUT_SECONDS), "-t", "ed25519", "-p", str(port)]
    try:
        _code, stdout, _stderr = await _run([*arguments, "--", host], time_limit)
    except TimeoutError, OSError:
        raise ValueError("ssh-keyscan did not answer") from None
    return parse_host_key(stdout, host, port)


def save_known_hosts(path: Path, host_key: HostKey) -> None:
    write_file_atomic(path, host_key.known_hosts_line.encode("ascii") + b"\n", 0o600)


def generate_key(private_path: Path, public_path: Path, key: GuardKey) -> str:
    """Erzeugt einen ed25519-Key (beide Dateien 0600); gibt den öffentlichen Teil zurück."""
    secret = Ed25519PrivateKey.generate()
    private = secret.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public = secret.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    public_line = f"{public.decode('ascii')} {key.comment}"
    write_file_atomic(private_path, private, 0o600)
    write_file_atomic(public_path, public_line.encode("ascii") + b"\n", 0o600)
    return public_line


def read_public_key(public_path: Path, key: GuardKey) -> str | None:
    try:
        line = public_path.read_text(encoding="ascii").strip()
    except FileNotFoundError, UnicodeDecodeError:
        return None
    parts = line.split()
    if len(parts) < 2 or parts[0] != KEY_TYPE or _key_blob_type(parts[1]) != KEY_TYPE:
        return None
    return f"{parts[0]} {parts[1]} {key.comment}"


def authorized_keys_line(public_line: str, addresses: Sequence[IpAddress], key: GuardKey) -> str:
    """Zeile für /boot/config/ssh/root/authorized_keys auf Groß (Konzept 5.6).

    Unraids Prüfung beim Speichern (checkKey in UserEdit.page, Unraid 7.3) akzeptiert sie.
    """
    sources = ",".join(str(address) for address in addresses)
    return f'restrict,from="{sources}",command="{key.guard_command}" {public_line}'
