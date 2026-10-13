"""Serverseitige Sessions (Konzept 5.3).

Die Session-ID hat 256 Bit Zufall und steht in der Datenbank nur als
SHA-256-Hash. Jede Session hat ein eigenes CSRF-Token.
"""

import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from twinraid.web.db import WebDatabase

SESSION_COOKIE = "__Host-twinraid_session"
MAX_LIFETIME_SECONDS = 12 * 60 * 60
# last_seen wird höchstens alle 30 Sekunden geschrieben, um die Datenbank zu schonen.
TOUCH_INTERVAL_SECONDS = 30.0
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Session:
    id_hash: str
    csrf_token: str
    created_at: float
    last_seen: float

    def expires_at(self, idle_seconds: float) -> float:
        return min(self.created_at + MAX_LIFETIME_SECONDS, self.last_seen + idle_seconds)


@dataclass(frozen=True)
class Lookup:
    session: Session | None
    expired: bool = False


class SessionStore:
    def __init__(
        self, db: WebDatabase, idle_seconds: float, clock: Callable[[], float] = time.time
    ) -> None:
        self._db = db
        self.idle_seconds = idle_seconds
        self._clock = clock

    def create(self) -> tuple[str, Session]:
        """Neue Session. Gibt das Cookie-Token (nur hier im Klartext) und die Session zurück."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        session = Session(_hash(token), secrets.token_urlsafe(32), now, now)
        with self._db.connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id_hash, csrf_token, created_at, last_seen) "
                "VALUES (?, ?, ?, ?)",
                (session.id_hash, session.csrf_token, session.created_at, session.last_seen),
            )
        return token, session

    def lookup(self, token: str | None, touch: bool = True) -> Lookup:
        """Session zum Cookie. touch=False: zählt nicht als Aktivität (Live-Datenstrom)."""
        if token is None or _TOKEN_RE.fullmatch(token) is None:
            return Lookup(None)
        return self.check(_hash(token), touch=touch)

    def check(self, id_hash: str, touch: bool = False, force: bool = False) -> Lookup:
        """Prüft eine Session; löscht sie, wenn sie abgelaufen ist.

        touch: letzte Aktivität setzen (höchstens alle 30 Sekunden).
        force: letzte Aktivität sofort setzen, z. B. am Ende eines Kopierjobs (Konzept 5.3).
        Die maximale Lebensdauer gilt immer.
        """
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT csrf_token, created_at, last_seen FROM sessions WHERE id_hash = ?",
                (id_hash,),
            ).fetchone()
            if row is None:
                return Lookup(None)
            session = Session(id_hash, str(row[0]), float(row[1]), float(row[2]))
            now = self._clock()
            if now >= session.expires_at(self.idle_seconds):
                connection.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
                return Lookup(None, expired=True)
            if force or (touch and now - session.last_seen >= TOUCH_INTERVAL_SECONDS):
                connection.execute(
                    "UPDATE sessions SET last_seen = ? WHERE id_hash = ?", (now, id_hash)
                )
                session = Session(id_hash, session.csrf_token, session.created_at, now)
        return Lookup(session)

    def delete(self, id_hash: str) -> None:
        with self._db.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))

    def delete_all(self) -> int:
        with self._db.connect() as connection:
            return connection.execute("DELETE FROM sessions").rowcount

    def purge_expired(self) -> None:
        now = self._clock()
        with self._db.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE created_at <= ? OR last_seen <= ?",
                (now - MAX_LIFETIME_SECONDS, now - self.idle_seconds),
            )
