"""Datenbankdatei des Web-Prozesses."""

import stat
from pathlib import Path

import pytest

from twinraid.web.db import DatabaseError, WebDatabase


def test_files_are_private(tmp_path: Path) -> None:
    database = WebDatabase(tmp_path / "web.sqlite")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO lockouts (ip_key, until) VALUES ('x', 1)")
    for path in tmp_path.glob("web.sqlite*"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_wrong_mode_is_corrected(tmp_path: Path) -> None:
    path = tmp_path / "web.sqlite"
    WebDatabase(path).initialize()
    path.chmod(0o644)
    WebDatabase(path).initialize()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlinked_database_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.sqlite"
    WebDatabase(target).initialize()
    (tmp_path / "web.sqlite").symlink_to(target)
    with pytest.raises(DatabaseError, match="regular file"):
        WebDatabase(tmp_path / "web.sqlite").initialize()


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database = WebDatabase(tmp_path / "web.sqlite")
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert {"sessions", "login_failures", "lockouts", "audit_log"} <= tables
