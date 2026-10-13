"""Login-Sperren (Konzept 5.3).

Pro IP: 5 Fehlversuche in 15 Minuten sperren diese IP für 15 Minuten. IPv6
wird pro /64-Netz gezählt. Global gibt es keine harte Sperre, nur eine
Verzögerung; die Begrenzung gleichzeitiger Prüfungen liegt in der Login-Route.
"""

import time
from collections.abc import Callable
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address

from twinraid.web.db import WebDatabase

WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60
MAX_FAILURES_PER_IP = 5
GLOBAL_SLOWDOWN_THRESHOLD = 20
GLOBAL_DELAY_SECONDS = 2.0


def normalize_ip(address: str) -> IPv4Address | IPv6Address:
    """IPv4-Adressen in IPv6-Schreibweise (::ffff:a.b.c.d) werden zu IPv4."""
    ip = ip_address(address)
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def ip_key(address: str) -> str:
    ip = normalize_ip(address)
    if isinstance(ip, IPv6Address):
        return str(IPv6Network(f"{ip}/64", strict=False))
    return str(ip)


class LoginLimiter:
    def __init__(self, db: WebDatabase, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    def locked_until(self, key: str) -> float | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT until FROM lockouts WHERE ip_key = ?", (key,)
            ).fetchone()
        if row is None or float(row[0]) <= self._clock():
            return None
        return float(row[0])

    def record_failure(self, key: str) -> bool:
        """Zählt einen Fehlversuch. True, wenn damit eine Sperre beginnt."""
        now = self._clock()
        with self._db.connect() as connection:
            connection.execute("INSERT INTO login_failures (ip_key, at) VALUES (?, ?)", (key, now))
            (count,) = connection.execute(
                "SELECT COUNT(*) FROM login_failures WHERE ip_key = ? AND at > ?",
                (key, now - WINDOW_SECONDS),
            ).fetchone()
            if int(count) < MAX_FAILURES_PER_IP:
                return False
            connection.execute(
                "INSERT OR REPLACE INTO lockouts (ip_key, until) VALUES (?, ?)",
                (key, now + LOCK_SECONDS),
            )
            connection.execute("DELETE FROM login_failures WHERE ip_key = ?", (key,))
            return True

    def record_success(self, key: str) -> None:
        with self._db.connect() as connection:
            connection.execute("DELETE FROM login_failures WHERE ip_key = ?", (key,))

    def global_failures(self) -> int:
        with self._db.connect() as connection:
            (count,) = connection.execute(
                "SELECT COUNT(*) FROM login_failures WHERE at > ?",
                (self._clock() - WINDOW_SECONDS,),
            ).fetchone()
        return int(count)

    def global_delay(self) -> float:
        return GLOBAL_DELAY_SECONDS if self.global_failures() >= GLOBAL_SLOWDOWN_THRESHOLD else 0.0

    def unlock_all(self) -> int:
        """Notfall über die Admin-CLI: alle Sperren und Zähler löschen."""
        with self._db.connect() as connection:
            locks = connection.execute("DELETE FROM lockouts").rowcount
            connection.execute("DELETE FROM login_failures")
        return locks

    def purge(self) -> None:
        now = self._clock()
        with self._db.connect() as connection:
            connection.execute("DELETE FROM login_failures WHERE at <= ?", (now - WINDOW_SECONDS,))
            connection.execute("DELETE FROM lockouts WHERE until <= ?", (now,))
