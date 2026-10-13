"""Schutzschichten vor dem Routing (Konzept 5.1 und 5.3).

Reihenfolge von außen nach innen:
SecurityHeaders → NetworkFilter → HostCheck → BodyLimit → OriginCheck → Auth (mit CSRF).
"""

import logging
import time
from collections.abc import Callable, Iterable
from ipaddress import IPv4Network, IPv6Network, ip_address

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from twinraid.web.ratelimit import normalize_ip

logger = logging.getLogger(__name__)

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), usb=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_BODY_BYTES = 16 * 1024
NETWORK_LOG_INTERVAL_SECONDS = 60.0


class SecurityHeadersMiddleware:
    """Setzt die Security-Header an jeder Antwort, auch an Fehlerantworten."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        is_api = str(scope.get("path", "")).startswith("/api")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in SECURITY_HEADERS}
                if is_api:
                    names.add(b"cache-control")
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                headers.extend(SECURITY_HEADERS)
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


class NetworkFilterMiddleware:
    """Weist Clients außerhalb von allowed_networks vor jeder Verarbeitung ab."""

    def __init__(
        self,
        app: ASGIApp,
        networks: Iterable[IPv4Network | IPv6Network],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._networks = tuple(networks)
        self._clock = clock
        self._last_logged: dict[str, float] = {}

    def is_allowed(self, address: str | None) -> bool:
        if address is None:
            return False
        try:
            ip = normalize_ip(address)
        except ValueError:
            return False
        return any(ip in network for network in self._networks)

    def _log(self, address: str) -> None:
        now = self._clock()
        if now - self._last_logged.get(address, float("-inf")) < NETWORK_LOG_INTERVAL_SECONDS:
            return
        if len(self._last_logged) > 1000:
            self._last_logged.clear()
        self._last_logged[address] = now
        logger.warning("Rejected request from %s: not in web.allowed_networks", address)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        client = scope.get("client")
        address = str(client[0]) if client else None
        if self.is_allowed(address):
            await self._app(scope, receive, send)
            return
        self._log(address or "unknown")
        await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)


def host_from_header(value: str) -> str | None:
    """Host ohne Port, kleingeschrieben; IPv6 ohne Klammern und normalisiert."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host, rest = value[1:end], value[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        try:
            return str(ip_address(host))
        except ValueError:
            return None
    host, _, port = value.partition(":")
    if port and not port.isdigit():
        return None
    return host or None


class HostCheckMiddleware:
    """Erlaubt nur Host-Header aus web.tls (Schutz gegen DNS-Rebinding)."""

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str]) -> None:
        self._app = app
        self._allowed = frozenset(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        raw = Headers(scope=scope).get("host")
        host = host_from_header(raw) if raw else None
        if host in self._allowed:
            await self._app(scope, receive, send)
            return
        await PlainTextResponse("invalid host", status_code=400)(scope, receive, send)


class _BodyTooLargeError(Exception):
    pass


class BodyLimitMiddleware:
    """Weist Requests mit mehr als 16 KiB Inhalt mit 413 ab."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None and (not length.isdigit() or int(length) > self._max):
            await self._too_large(scope, receive, send)
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, counting_receive, tracking_send)
        except _BodyTooLargeError:
            if not started:
                await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": "request_too_large"}, status_code=413)
        await response(scope, receive, send)


def expected_origin(scope: Scope) -> str | None:
    host = Headers(scope=scope).get("host")
    return f"https://{host.strip().lower()}" if host else None


class OriginCheckMiddleware:
    """Zustandsändernde Requests brauchen einen passenden Origin-Header."""

    def __init__(self, app: ASGIApp, on_reject: Callable[[Scope], None] | None = None) -> None:
        self._app = app
        self._on_reject = on_reject

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self._app(scope, receive, send)
            return
        origin = Headers(scope=scope).get("origin")
        expected = expected_origin(scope)
        if origin is not None and expected is not None and origin.strip().lower() == expected:
            await self._app(scope, receive, send)
            return
        if self._on_reject is not None:
            self._on_reject(scope)
        await JSONResponse({"detail": "origin_rejected"}, status_code=403)(scope, receive, send)
