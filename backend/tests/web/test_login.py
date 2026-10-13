"""Login, Sessions, CSRF und Logout (Konzept 5.3)."""

import sqlite3
from pathlib import Path

import pytest

from tests.support.passwords import TEST_PASSWORD
from tests.web.helpers import FakeClock, FakeWorker, login, make_app, make_client
from twinraid.web.sessions import SESSION_COOKIE


def _audit(tmp_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        return db.execute("SELECT action, result FROM audit_log ORDER BY id").fetchall()


# --- Login ------------------------------------------------------------------------


def test_successful_login_sets_secure_session_cookie(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    response = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "secure", "path=/", "samesite=strict", "max-age=43200"} <= attributes
    assert not any(a.startswith("domain") for a in attributes)
    body = response.json()
    assert set(body) == {"csrf_token", "expires_at", "idle_timeout_seconds"}
    assert body["idle_timeout_seconds"] == 10 * 60
    assert client.get("/api/status").status_code == 200
    assert _audit(tmp_path) == [("login", "success")]


def test_wrong_password(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    response = client.post("/api/auth/login", json={"password": "falsches-passwort"})
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_password"}
    assert "set-cookie" not in response.headers
    assert client.get("/api/status").status_code == 401
    assert _audit(tmp_path) == [("login", "invalid_password")]


def test_password_not_set(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path, FakeWorker(password=None)))
    assert client.get("/api/auth/state").json()["password_set"] is False
    response = client.post("/api/auth/login", json={"password": "irgendein-passwort"})
    assert response.status_code == 401
    assert response.json() == {"detail": "password_not_set"}
    assert _audit(tmp_path) == [("login", "password_not_set")]


def test_worker_unavailable(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path, FakeWorker(available=False)))
    response = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 503
    assert client.get("/api/auth/state").json()["password_set"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"password": ""},
        {"password": "x" * 1025},
        {"password": 12345},
        {"password": TEST_PASSWORD, "user": "admin"},
        {},
        ["x"],
    ],
)
def test_invalid_login_requests_do_not_echo_input(tmp_path: Path, body: object) -> None:
    worker = FakeWorker()
    client = make_client(make_app(tmp_path, worker))
    response = client.post("/api/auth/login", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
    assert TEST_PASSWORD not in response.text
    assert worker.verify_calls == 0


def test_login_always_creates_new_session_id(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    login(client)
    first = client.cookies[SESSION_COOKIE]
    login(client)
    second = client.cookies[SESSION_COOKIE]
    assert first != second
    replay = make_client(make_app(tmp_path))
    replay.cookies.set(SESSION_COOKIE, first)
    assert replay.get("/api/status").status_code == 401


def test_auth_state(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    assert client.get("/api/auth/state").json() == {
        "authenticated": False,
        "mode": "mock",
        "password_set": True,
    }
    login(client)
    assert client.get("/api/auth/state").json()["authenticated"] is True


def test_session_ids_are_stored_hashed(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    login(client)
    token = client.cookies[SESSION_COOKIE]
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        stored = [row[0] for row in db.execute("SELECT id_hash FROM sessions")]
    assert len(stored) == 1
    assert token not in stored[0]
    assert len(stored[0]) == 64


@pytest.mark.parametrize("cookie", ["", "kurz", "a" * 43 + "!", "a" * 200, "../../etc"])
def test_garbage_cookies_are_rejected(tmp_path: Path, cookie: str) -> None:
    client = make_client(make_app(tmp_path))
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/api/status").status_code == 401


# --- CSRF -------------------------------------------------------------------------


def test_logout_requires_csrf_token(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    csrf = login(client)
    for headers in ({}, {"X-CSRF-Token": "falsch"}, {"X-CSRF-Token": csrf[:-1]}):
        response = client.post("/api/auth/logout", headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "csrf_rejected"}
    assert client.get("/api/status").status_code == 200
    assert ("csrf_check", "rejected") in _audit(tmp_path)


def test_csrf_token_of_other_session_is_rejected(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    alice = make_client(app)
    bob = make_client(app)
    login(alice)
    bob_csrf = login(bob)
    response = alice.post("/api/auth/logout", headers={"X-CSRF-Token": bob_csrf})
    assert response.status_code == 403


def test_session_endpoint_returns_csrf_token(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    csrf = login(client)
    assert client.get("/api/auth/session").json()["csrf_token"] == csrf


# --- Logout -----------------------------------------------------------------------


def test_logout_deletes_session_on_server(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    csrf = login(client)
    token = client.cookies[SESSION_COOKIE]
    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert (
        "Max-Age=0" in response.headers["set-cookie"]
        or "max-age=0" in response.headers["set-cookie"].lower()
    )
    assert client.get("/api/status").status_code == 401
    replay = make_client(make_app(tmp_path))
    replay.cookies.set(SESSION_COOKIE, token)
    assert replay.get("/api/status").status_code == 401
    assert _audit(tmp_path)[-1] == ("logout", "success")


# --- Zeitlimits ---------------------------------------------------------------------


def test_idle_timeout(tmp_path: Path) -> None:
    clock = FakeClock()
    client = make_client(make_app(tmp_path, clock=clock))
    login(client)
    clock.advance(9 * 60)
    assert client.get("/api/status").status_code == 200
    clock.advance(9 * 60)
    assert client.get("/api/status").status_code == 200
    clock.advance(10 * 60 + 1)
    assert client.get("/api/status").status_code == 401
    assert ("session", "expired") in _audit(tmp_path)


def test_maximum_lifetime_of_12_hours(tmp_path: Path) -> None:
    clock = FakeClock()
    client = make_client(make_app(tmp_path, clock=clock))
    login(client)
    # 79 × 9 Minuten = 11 Stunden 51 Minuten, jeweils innerhalb des Inaktivitäts-Timeouts.
    for _ in range(79):
        clock.advance(9 * 60)
        assert client.get("/api/status").status_code == 200
    clock.advance(9 * 60)
    assert client.get("/api/status").status_code == 401


def test_set_password_style_session_purge(tmp_path: Path) -> None:
    """Die Admin-CLI löscht beim Passwortwechsel alle Sessions in der Datenbank."""
    app = make_app(tmp_path)
    client = make_client(app)
    login(client)
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        db.execute("DELETE FROM sessions")
    assert client.get("/api/status").status_code == 401
