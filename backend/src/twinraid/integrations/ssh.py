"""SSH zu Unraid Groß mit dem Key „power“ (Konzept 3.4 und 5.6).

Auf Groß erzwingt authorized_keys den Aufruf von guard.sh; der hier gesendete
Text landet dort nur in SSH_ORIGINAL_COMMAND und wird mit festen Mustern
verglichen. Erlaubt sind "check", "poweroff" und "vm-shutdown <UUID>".

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
COMMAND_TIMEOUT_SECONDS = 30.0
KEYSCAN_TIMEOUT_SECONDS = 15.0
REMOTE_USER = "root"
KEY_TYPE = "ssh-ed25519"
POWER_KEY_COMMENT = "twinraid-power"
GUARD_COMMAND = "/bin/bash /boot/config/twinraid/guard.sh power"
# guard.sh antwortet auf "check" mit "twinraid-guard <Version> power ok".
GUARD_CHECK_PATTERN = re.compile(r"twinraid-guard (\d+) power ok", re.ASCII)
# Exit-Code von guard.sh für abgelehnte Anfragen; ssh selbst meldet Fehler mit 255.
GUARD_DENIED_EXIT = 2
SSH_ERROR_EXIT = 255
_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII
)
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
    def of(cls, config: TwinRaidConfig, paths: DataPaths) -> SshTarget:
        target = config.servers.target
        return cls(str(target.host), target.ssh_port, paths.ssh_power_key, paths.target_known_hosts)


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
        # Feste Umgebung: englische Meldungen für classify(), keine geerbten Variablen.
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
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


class SshPower:
    """Die drei Anfragen an guard.sh im Modus "power"."""

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
        stdout = await self._request(["check"])
        return stdout.strip().splitlines()[0] if stdout.strip() else ""

    async def poweroff(self) -> None:
        await self._request(["poweroff"])

    async def vm_shutdown(self, uuid: str) -> None:
        """ACPI-Signal an die VM (virsh shutdown); erzwingt nichts."""
        if _UUID_PATTERN.fullmatch(uuid) is None:
            raise SshError(ActionOutcome.INTERNAL_ERROR, "invalid VM UUID")
        await self._request(["vm-shutdown", uuid])

    async def _request(self, request: list[str]) -> str:
        if not self.target.key.is_file():
            raise SshError(ActionOutcome.SSH_KEY_MISSING, "no SSH key 'power'")
        if not self.target.known_hosts.is_file():
            raise SshError(ActionOutcome.SSH_HOST_UNTRUSTED, "host key not confirmed")
        arguments = ssh_arguments(self.target, request, self._binary)
        try:
            returncode, stdout, stderr = await _run(arguments, self._timeout)
        except TimeoutError:
            raise SshError(ActionOutcome.SSH_UNREACHABLE, "ssh timed out") from None
        except OSError:
            raise SshError(ActionOutcome.SSH_FAILED, "ssh could not be started") from None
        error = classify(returncode, stderr)
        if error is not None:
            raise error
        return stdout


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


def generate_power_key(private_path: Path, public_path: Path) -> str:
    """Erzeugt den ed25519-Key „power“ (beide Dateien 0600); gibt den öffentlichen Teil zurück."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    public_line = f"{public.decode('ascii')} {POWER_KEY_COMMENT}"
    write_file_atomic(private_path, private, 0o600)
    write_file_atomic(public_path, public_line.encode("ascii") + b"\n", 0o600)
    return public_line


def read_public_key(public_path: Path) -> str | None:
    try:
        line = public_path.read_text(encoding="ascii").strip()
    except FileNotFoundError, UnicodeDecodeError:
        return None
    parts = line.split()
    if len(parts) < 2 or parts[0] != KEY_TYPE or _key_blob_type(parts[1]) != KEY_TYPE:
        return None
    return f"{parts[0]} {parts[1]} {POWER_KEY_COMMENT}"


def authorized_keys_line(public_line: str, addresses: Sequence[IpAddress]) -> str:
    """Zeile für /boot/config/ssh/root/authorized_keys auf Groß (Konzept 5.6).

    Unraids Prüfung beim Speichern (checkKey in UserEdit.page, Unraid 7.3) akzeptiert sie.
    """
    sources = ",".join(str(address) for address in addresses)
    return f'restrict,from="{sources}",command="{GUARD_COMMAND}" {public_line}'
