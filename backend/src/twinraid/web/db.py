"""SQLite-Datenbank des Web-Prozesses: Sessions, Login-Sperren, Audit-Log (Konzept 5.3).

Liegt in /data/web/web.sqlite, gehört dem Web-Benutzer und hat die Rechte 0600.
Die Admin-CLI greift nur als derselbe Benutzer darauf zu.
"""

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (
    ip_key TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures (ip_key, at);
CREATE INDEX IF NOT EXISTS login_failures_at ON login_failures (at);
CREATE TABLE IF NOT EXISTS lockouts (
    ip_key TEXT PRIMARY KEY,
    until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    client_ip TEXT,
    action TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log (at);
CREATE TABLE IF NOT EXISTS acknowledgements (
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (kind, ref)
);
"""


class DatabaseError(Exception):
    """Die Datenbankdatei ist unsicher oder unbrauchbar."""


class WebDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Prüft die Datei, legt das Schema an und schaltet WAL ein."""
        self._check_file()
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        self._check_file()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Eine Verbindung pro Vorgang; Änderungen werden am Ende festgeschrieben."""
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            # Gelöschte Einträge (z. B. beendete Sessions) werden überschrieben.
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def _check_file(self) -> None:
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise DatabaseError(f"{path} must be a regular file, not a link")
            if info.st_uid != os.geteuid():
                raise DatabaseError(
                    f"{path} is owned by uid {info.st_uid}, expected {os.geteuid()}"
                )
            if stat.S_IMODE(info.st_mode) != 0o600:
                path.chmod(0o600)
