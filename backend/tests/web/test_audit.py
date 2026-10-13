"""Audit-Log: Inhalt, Aufbewahrung und keine Secrets (Konzept 5.3)."""

import sqlite3
from pathlib import Path

import pytest

from tests.web.helpers import FakeClock, login, make_app, make_client
from twinraid.web import audit as audit_module
from twinraid.web.audit import AuditAction, AuditLog, AuditResult
from twinraid.web.db import WebDatabase
from twinraid.web.sessions import SESSION_COOKIE

WRONG_PASSWORD = "canary-wrong-password-9x7"


@pytest.fixture
def database(tmp_path: Path) -> WebDatabase:
    db = WebDatabase(tmp_path / "web.sqlite")
    db.initialize()
    return db


def test_entries_have_time_ip_action_result(database: WebDatabase) -> None:
    clock = FakeClock()
    log = AuditLog(database, clock)
    log.record(AuditAction.LOGIN, AuditResult.SUCCESS, "192.168.20.5")
    (entry,) = log.recent()
    assert entry.at.timestamp() == clock.now
    assert entry.at.tzinfo is not None
    assert (entry.client_ip, entry.action, entry.result) == ("192.168.20.5", "login", "success")


def test_newest_first_and_limit(database: WebDatabase) -> None:
    log = AuditLog(database)
    for result in (AuditResult.INVALID_PASSWORD, AuditResult.SUCCESS):
        log.record(AuditAction.LOGIN, result, None)
    assert [e.result for e in log.recent()] == ["success", "invalid_password"]
    assert len(log.recent(limit=1)) == 1


def test_dedupe_within_one_minute(database: WebDatabase) -> None:
    clock = FakeClock()
    log = AuditLog(database, clock)
    for _ in range(3):
        log.record(AuditAction.ORIGIN_CHECK, AuditResult.REJECTED, "1.2.3.4", dedupe=True)
    log.record(AuditAction.ORIGIN_CHECK, AuditResult.REJECTED, "5.6.7.8", dedupe=True)
    clock.advance(61)
    log.record(AuditAction.ORIGIN_CHECK, AuditResult.REJECTED, "1.2.3.4", dedupe=True)
    assert len(log.recent()) == 3


def test_retention_180_days(database: WebDatabase) -> None:
    clock = FakeClock()
    log = AuditLog(database, clock)
    log.record(AuditAction.LOGIN, AuditResult.SUCCESS, None)
    clock.advance(179 * 24 * 3600)
    log.record(AuditAction.LOGOUT, AuditResult.SUCCESS, None)
    clock.advance(2 * 24 * 3600)
    log.purge()
    assert [e.action for e in log.recent()] == ["logout"]


def test_maximum_entries(database: WebDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_module, "MAX_ENTRIES", 3)
    log = AuditLog(database)
    for _ in range(5):
        log.record(AuditAction.LOGIN, AuditResult.SUCCESS, None)
    log.record(AuditAction.LOGOUT, AuditResult.SUCCESS, None)
    log.purge()
    entries = log.recent()
    assert len(entries) == 3
    assert entries[0].action == "logout"


def test_secrets_are_never_stored_or_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(make_app(tmp_path))
    client.post("/api/auth/login", json={"password": WRONG_PASSWORD})
    csrf = login(client)
    session_token = client.cookies[SESSION_COOKIE]
    client.post("/api/auth/logout", headers={"X-CSRF-Token": "falsch"})
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    database_file = b"".join(
        path.read_bytes() for path in tmp_path.glob("web.sqlite*") if path.is_file()
    )
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        audit_rows = repr(db.execute("SELECT * FROM audit_log").fetchall())
    output = capsys.readouterr()
    logged = output.out + output.err + caplog.text

    # Passwörter und Session-IDs stehen nirgends in der Datenbankdatei.
    for secret in (WRONG_PASSWORD, "richtiges-test-passwort-42", session_token):
        assert secret.encode() not in database_file, secret
    # Kein Secret im Audit-Log oder im Log. Das CSRF-Token liegt nur in der
    # Session-Tabelle, weil der Server es zum Vergleich braucht.
    for secret in (WRONG_PASSWORD, "richtiges-test-passwort-42", csrf, session_token):
        assert secret not in audit_rows, secret
        assert secret not in logged, secret


def test_deleted_sessions_leave_no_csrf_token_behind(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    csrf = login(client)
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_file = (tmp_path / "web.sqlite").read_bytes()
    assert csrf.encode() not in database_file
