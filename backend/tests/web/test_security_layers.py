"""Schutzschichten vor dem Routing (Konzept 5.1 und 5.3)."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from tests.web.helpers import BASE_URL, login, make_app, make_client
from twinraid.web.app import WebApp
from twinraid.web.security import (
    CONTENT_SECURITY_POLICY,
    BodyLimitMiddleware,
    host_from_header,
)

EXPECTED_HEADERS = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


@pytest.fixture
def app(tmp_path: Path) -> WebApp:
    return make_app(tmp_path)


# --- Netzfilter ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/", "/assets/app.js", "/api/auth/state", "/x"])
def test_foreign_network_is_rejected_everywhere(app: WebApp, path: str) -> None:
    response = make_client(app, ip="10.1.2.3").get(path)
    assert response.status_code == 403
    assert response.text == "forbidden"


@pytest.mark.parametrize("ip", ["192.168.20.5", "192.168.10.99", "::ffff:192.168.20.5"])
def test_allowed_networks_pass(app: WebApp, ip: str) -> None:
    assert make_client(app, ip=ip).get("/health").status_code == 200


@pytest.mark.parametrize("ip", ["fd00::1", "testclient", "", "192.168.21.5"])
def test_other_clients_are_rejected(app: WebApp, ip: str) -> None:
    assert make_client(app, ip=ip).get("/health").status_code == 403


def test_forwarded_headers_are_ignored(app: WebApp) -> None:
    client = make_client(app, ip="10.1.2.3")
    headers = {"X-Forwarded-For": "192.168.20.5", "X-Real-IP": "192.168.20.5"}
    assert client.get("/health", headers=headers).status_code == 403


def test_forwarded_headers_do_not_change_audit_ip(app: WebApp, tmp_path: Path) -> None:
    client = make_client(app)
    client.post(
        "/api/auth/login", json={"password": "falsch"}, headers={"X-Forwarded-For": "1.2.3.4"}
    )
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        ips = {row[0] for row in db.execute("SELECT client_ip FROM audit_log")}
    assert ips == {"192.168.20.5"}


def test_network_rejection_is_logged_rate_limited(
    app: WebApp, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(app, ip="10.1.2.3")
    for _ in range(5):
        client.get("/health")
    rejected = [r for r in caplog.records if "not in web.allowed_networks" in r.getMessage()]
    assert len(rejected) == 1


# --- Security-Header -------------------------------------------------------------


def _assert_headers(headers: dict[str, str] | object) -> None:
    for name, value in EXPECTED_HEADERS.items():
        assert headers[name] == value, name  # type: ignore[index]


@pytest.mark.parametrize(
    ("ip", "host", "path", "status"),
    [
        ("192.168.20.5", None, "/health", 200),
        ("192.168.20.5", None, "/", 200),
        ("192.168.20.5", None, "/assets/app.js", 200),
        ("192.168.20.5", None, "/api/status", 401),
        ("10.1.2.3", None, "/health", 403),
        ("192.168.20.5", "evil.example", "/health", 400),
    ],
)
def test_headers_on_every_response(
    app: WebApp, ip: str, host: str | None, path: str, status: int
) -> None:
    headers = {"Host": host} if host else {}
    response = make_client(app, ip=ip).get(path, headers=headers)
    assert response.status_code == status
    _assert_headers(response.headers)


def test_headers_on_server_error(app: WebApp) -> None:
    @app.api.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("kaputt")

    client = make_client(app)
    login(client)
    raw = TestClient(
        app, base_url=BASE_URL, client=("192.168.20.5", 1), raise_server_exceptions=False
    )
    raw.cookies = client.cookies
    response = raw.get("/api/boom")
    assert response.status_code == 500
    _assert_headers(response.headers)


def test_api_responses_are_not_cached(app: WebApp) -> None:
    client = make_client(app)
    assert client.get("/api/auth/state").headers["cache-control"] == "no-store"
    assert client.get("/api/status").headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_csp_forbids_inline_and_foreign_sources() -> None:
    directives = dict(d.strip().split(" ", 1) for d in CONTENT_SECURITY_POLICY.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY


# --- Host-Prüfung -------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["192.168.10.20:8443", "192.168.10.20", "192.168.10.20:443"],
)
def test_allowed_host_headers(app: WebApp, host: str) -> None:
    assert make_client(app).get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:8443", "192.168.10.21:8443", "192.168.10.20:abc", "", "[::1]"],
)
def test_rejected_host_headers(app: WebApp, host: str) -> None:
    response = make_client(app).get("/health", headers={"Host": host})
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.Home.Arpa:8443", "example.home.arpa"),
        ("[FD00:0::20]:8443", "fd00::20"),
        ("[fd00::20]", "fd00::20"),
        ("192.168.10.20", "192.168.10.20"),
        ("[fd00::20", None),
        ("[fd00::20]x", None),
        ("[nicht-ip]", None),
        (":8443", None),
        ("host:port", None),
    ],
)
def test_host_parsing(raw: str, expected: str | None) -> None:
    assert host_from_header(raw) == expected


# --- Größenlimit ----------------------------------------------------------------------


def test_large_body_is_rejected(app: WebApp) -> None:
    response = make_client(app).post(
        "/api/auth/login", content=b"x" * (16 * 1024 + 1), headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 413
    _assert_headers(response.headers)


def test_body_at_limit_is_processed(app: WebApp) -> None:
    body = b'{"password": "' + b"x" * (16 * 1024 - 20) + b'"}'
    response = make_client(app).post(
        "/api/auth/login", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_streamed_body_without_length_is_limited() -> None:
    """Ohne Content-Length zählt die Middleware die tatsächlich empfangenen Bytes."""
    seen: list[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunks = [b"x" * 10_000, b"x" * 10_000]

    async def receive() -> Message:
        body = chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message: Message) -> None:
        seen.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    asyncio.run(BodyLimitMiddleware(inner)(scope, receive, send))
    assert seen[0]["status"] == 413


# --- Origin-Prüfung -------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [None, "https://evil.example", "null", "http://192.168.10.20:8443", "https://192.168.10.20"],
)
def test_state_changing_requests_need_matching_origin(
    tmp_path: Path, app: WebApp, origin: str | None
) -> None:
    client = make_client(app, origin=False)
    headers = {"Origin": origin} if origin is not None else {}
    response = client.post("/api/auth/login", json={"password": "x"}, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "origin_rejected"}


def test_origin_rejections_are_audited_once_per_minute(app: WebApp, tmp_path: Path) -> None:
    client = make_client(app, origin=False)
    for _ in range(3):
        client.post("/api/auth/login", json={"password": "x"})
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        rows = db.execute(
            "SELECT action, result FROM audit_log WHERE action = 'origin_check'"
        ).fetchall()
    assert rows == [("origin_check", "rejected")]


def test_get_requests_need_no_origin(app: WebApp) -> None:
    assert make_client(app, origin=False).get("/api/auth/state").status_code == 200


def test_matching_origin_passes(app: WebApp) -> None:
    response = make_client(app).post("/api/auth/login", json={"password": "falsches-pw"})
    assert response.status_code == 401
