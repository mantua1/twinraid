"""Unraid-GraphQL-API (Konzept 3.5). In Phase 3 nur lesend: VM-Status von Groß.

Anfragen gehen per POST an <scheme>://<host>:<port>/graphql mit dem Key im
Header x-api-key. Bei https wird statt einer Zertifikatskette der bestätigte
SHA-256-Fingerabdruck geprüft, bevor der Key gesendet wird. Weiterleitungen
werden nicht verfolgt. Ursachen für Fehler kommen aus einer festen Liste;
Antworttexte von Unraid gelangen weder in den Status noch ins Log.
"""

import asyncio
import hmac
import http.client
import json
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

from twinraid import __version__
from twinraid.config.models import TargetServerConfig
from twinraid.shared.files import write_file_atomic
from twinraid.shared.status import StatusReason
from twinraid.shared.tls import der_fingerprint

TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 256 * 1024
GRAPHQL_PATH = "/graphql"
VM_QUERY = "query TwinRaidVmStatus { vms { domains { name state } } }"
# Zeichen wie bei Unraid (header.strategy.ts); die Länge begrenzen wir zusätzlich.
API_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,512}", re.ASCII)
_FINGERPRINT_PATTERN = re.compile(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", re.ASCII)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class UnraidApiError(Exception):
    """Die API-Abfrage ist gescheitert; reason sagt, warum."""

    def __init__(self, reason: StatusReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ApiEndpoint:
    host: str
    port: int
    encrypted: bool

    @classmethod
    def of(cls, server: TargetServerConfig) -> ApiEndpoint:
        webgui = server.webgui
        return cls(str(server.host), webgui.effective_port, webgui.encrypted)

    @property
    def url(self) -> str:
        scheme = "https" if self.encrypted else "http"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}{GRAPHQL_PATH}"


def normalize_fingerprint(value: str) -> str:
    """Nimmt AB:CD:… oder abcd… an und gibt AB:CD:… zurück."""
    compact = value.strip().replace(":", "").upper()
    if not re.fullmatch(r"[0-9A-F]{64}", compact, re.ASCII):
        raise ValueError("a SHA-256 fingerprint has 64 hex digits, e.g. AB:CD:…")
    return ":".join(compact[i : i + 2] for i in range(0, 64, 2))


class Credentials:
    """API-Key und bestätigter Fingerabdruck in /data/secrets.

    Beide werden bei jeder Abfrage neu gelesen; nach einer Änderung ist kein Neustart nötig.
    """

    def __init__(self, key_path: Path, pin_path: Path) -> None:
        self._key_path = key_path
        self._pin_path = pin_path

    def api_key(self) -> str | None:
        try:
            key = self._key_path.read_text(encoding="ascii").strip()
        except FileNotFoundError, UnicodeDecodeError:
            return None
        return key if API_KEY_PATTERN.fullmatch(key) else None

    def pinned_fingerprint(self) -> str | None:
        try:
            value = self._pin_path.read_text(encoding="ascii").strip()
        except FileNotFoundError, UnicodeDecodeError:
            return None
        return value if _FINGERPRINT_PATTERN.fullmatch(value) else None

    def save_api_key(self, key: str) -> None:
        if API_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError(
                "the API key may only contain letters, digits, '-' and '_' (at most 512)"
            )
        write_file_atomic(self._key_path, key.encode("ascii") + b"\n", 0o600)

    def save_fingerprint(self, fingerprint: str) -> None:
        write_file_atomic(
            self._pin_path, normalize_fingerprint(fingerprint).encode("ascii") + b"\n", 0o600
        )


def _tls_context() -> ssl.SSLContext:
    # Unraid nutzt ein selbstsigniertes Zertifikat. Geprüft wird nicht die Kette,
    # sondern der Fingerabdruck (Konzept 3.5), und zwar vor dem Senden des Keys.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def presented_fingerprint(endpoint: ApiEndpoint, timeout: float = TIMEOUT_SECONDS) -> str:
    """SHA-256-Fingerabdruck des Zertifikats, das der Server gerade ausliefert (nur Handshake)."""
    connection = http.client.HTTPSConnection(
        endpoint.host, endpoint.port, timeout=timeout, context=_tls_context()
    )
    try:
        connection.connect()
        der = _peer_certificate(connection)
    except (OSError, ssl.SSLError) as exc:
        raise UnraidApiError(StatusReason.API_UNREACHABLE, type(exc).__name__) from None
    finally:
        connection.close()
    return der_fingerprint(der)


def _peer_certificate(connection: http.client.HTTPConnection) -> bytes:
    sock = connection.sock
    der = sock.getpeercert(binary_form=True) if isinstance(sock, ssl.SSLSocket) else None
    if not der:
        raise UnraidApiError(StatusReason.API_ERROR, "no certificate was presented")
    return der


def _classify_errors(errors: object) -> StatusReason:
    """Ordnet GraphQL-Fehler einer Ursache zu (Codes von NestJS, ersatzweise der Text)."""
    if not isinstance(errors, list):
        return StatusReason.API_ERROR
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        message = str(error.get("message", "")).lower()
        if code == "UNAUTHENTICATED" or "unauthorized" in message or "api key" in message:
            return StatusReason.API_KEY_INVALID
        if code == "FORBIDDEN" or "forbidden" in message:
            return StatusReason.API_FORBIDDEN
    return StatusReason.API_ERROR


class UnraidApi:
    def __init__(
        self,
        endpoint: ApiEndpoint,
        credentials: Credentials,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = endpoint
        self._credentials = credentials
        self._timeout = timeout

    async def vm_state(self, vm_name: str) -> str | None:
        """libvirt-Zustand der VM (z. B. RUNNING); None, wenn es die VM nicht gibt."""
        domains = await asyncio.to_thread(self.vm_states)
        return domains.get(vm_name)

    def vm_states(self) -> dict[str, str]:
        data = self._query(VM_QUERY)
        vms = data.get("vms")
        domains = vms.get("domains") if isinstance(vms, dict) else None
        if not isinstance(domains, list):
            raise UnraidApiError(StatusReason.API_ERROR, "unexpected answer to the VM query")
        states: dict[str, str] = {}
        for domain in domains:
            if isinstance(domain, dict):
                name, state = domain.get("name"), domain.get("state")
                if isinstance(name, str) and isinstance(state, str):
                    states[name] = state
        return states

    def _connection(self) -> http.client.HTTPConnection:
        endpoint = self.endpoint
        if endpoint.encrypted:
            return http.client.HTTPSConnection(
                endpoint.host, endpoint.port, timeout=self._timeout, context=_tls_context()
            )
        return http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=self._timeout)

    def _query(self, query: str) -> dict[str, object]:
        key = self._credentials.api_key()
        if key is None:
            raise UnraidApiError(StatusReason.API_KEY_MISSING, "no API key is set")
        pin: str | None = None
        if self.endpoint.encrypted:
            pin = self._credentials.pinned_fingerprint()
            if pin is None:
                raise UnraidApiError(StatusReason.API_CERT_UNTRUSTED, "fingerprint not confirmed")
        connection = self._connection()
        try:
            try:
                connection.connect()
            except (OSError, ssl.SSLError) as exc:
                raise UnraidApiError(StatusReason.API_UNREACHABLE, type(exc).__name__) from None
            if pin is not None:
                presented = der_fingerprint(_peer_certificate(connection))
                if not hmac.compare_digest(presented, pin):
                    raise UnraidApiError(StatusReason.API_CERT_CHANGED, "fingerprint differs")
            status, body = self._post(connection, key, query)
        finally:
            connection.close()
        return self._parse(status, body)

    def _post(
        self, connection: http.client.HTTPConnection, key: str, query: str
    ) -> tuple[int, bytes]:
        payload = json.dumps({"query": query}).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"TwinRaid/{__version__}",
            "x-api-key": key,
        }
        try:
            connection.request("POST", GRAPHQL_PATH, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise UnraidApiError(StatusReason.API_UNREACHABLE, type(exc).__name__) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise UnraidApiError(StatusReason.API_ERROR, "answer is too large")
        return response.status, body

    @staticmethod
    def _parse(status: int, body: bytes) -> dict[str, object]:
        if status in _REDIRECTS:
            raise UnraidApiError(StatusReason.API_REDIRECTED, f"HTTP {status}")
        if status == 401:
            raise UnraidApiError(StatusReason.API_KEY_INVALID, "HTTP 401")
        if status == 403:
            raise UnraidApiError(StatusReason.API_FORBIDDEN, "HTTP 403")
        try:
            document = json.loads(body)
        except ValueError:
            raise UnraidApiError(StatusReason.API_ERROR, f"HTTP {status}, no JSON") from None
        if not isinstance(document, dict):
            raise UnraidApiError(StatusReason.API_ERROR, f"HTTP {status}, unexpected JSON")
        if document.get("errors"):
            reason = _classify_errors(document["errors"])
            raise UnraidApiError(reason, f"HTTP {status}, GraphQL error")
        data = document.get("data")
        if status != 200 or not isinstance(data, dict):
            raise UnraidApiError(StatusReason.API_ERROR, f"HTTP {status}")
        return data
