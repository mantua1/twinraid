"""Audit-Log (Konzept 5.3).

Aktion und Ergebnis stammen aus festen Listen. Kein Feld übernimmt Text aus
Requests. Passwörter, Session-IDs, Tokens und Cookies werden nie gespeichert.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from twinraid.web.db import WebDatabase

logger = logging.getLogger(__name__)

RETENTION_SECONDS = 180 * 24 * 60 * 60
MAX_ENTRIES = 50_000
# Gleiche Abweisungen derselben IP höchstens einmal pro Minute eintragen.
DEDUPE_SECONDS = 60.0


class AuditAction(StrEnum):
    LOGIN = "login"
    LOGOUT = "logout"
    SESSION = "session"
    CSRF_CHECK = "csrf_check"
    ORIGIN_CHECK = "origin_check"
    CLI_SET_PASSWORD = "cli_set_password"  # noqa: S105 - Aktionsname, kein Passwort
    CLI_RENEW_CERT = "cli_renew_cert"
    CLI_UNLOCK = "cli_unlock"
    # Nur bis v0.4.2 (API-Key von Groß); alte Einträge bleiben lesbar.
    CLI_SET_API_KEY = "cli_set_api_key"
    CLI_TRUST_API_CERT = "cli_trust_api_cert"
    CA_CHANGE_NOTICE = "ca_change_notice"
    CLI_INIT_SSH_KEYS = "cli_init_ssh_keys"
    CLI_TRUST_HOST_KEY = "cli_trust_host_key"
    # Anforderung einer Aktion über die Oberfläche (Konzept 5.3, ab Phase 4).
    WAKE_TARGET = "wake_target"
    SHUTDOWN_TARGET = "shutdown_target"
    SHUTDOWN_TARGET_FORCE_VM = "shutdown_target_force_vm"
    VM_START = "vm_start"
    VM_STOP = "vm_stop"
    VM_FORCE_STOP = "vm_force_stop"
    # Ab v0.5.1 (Konzept 5.3): Probelauf und Sync, dazu twinraid-admin sync-check.
    SYNC_DRY_RUN = "sync_dry_run"
    SYNC_START = "sync_start"
    SYNC_CANCEL = "sync_cancel"
    SYNC_CONFIRM_DELETIONS = "sync_confirm_deletions"
    CLI_SYNC_CHECK = "cli_sync_check"


class AuditResult(StrEnum):
    SUCCESS = "success"
    INVALID_PASSWORD = "invalid_password"  # noqa: S105 - Ergebnisname, kein Passwort
    PASSWORD_NOT_SET = "password_not_set"  # noqa: S105 - Ergebnisname, kein Passwort
    LOCKED = "locked"
    LOCKOUT_STARTED = "lockout_started"
    BUSY = "busy"
    EXPIRED = "expired"
    REJECTED = "rejected"
    ACKNOWLEDGED = "acknowledged"
    # Der Worker hat die Aktion angenommen; wie sie ausging, steht im Aktionslog.
    REQUESTED = "requested"


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    client_ip: str | None
    action: str
    result: str


class AuditLog:
    def __init__(self, db: WebDatabase, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    def record(
        self,
        action: AuditAction,
        result: AuditResult,
        client_ip: str | None,
        dedupe: bool = False,
    ) -> None:
        now = self._clock()
        with self._db.connect() as connection:
            if dedupe:
                duplicate = connection.execute(
                    "SELECT 1 FROM audit_log WHERE client_ip IS ? AND action = ? AND result = ? "
                    "AND at > ? LIMIT 1",
                    (client_ip, action.value, result.value, now - DEDUPE_SECONDS),
                ).fetchone()
                if duplicate is not None:
                    return
            connection.execute(
                "INSERT INTO audit_log (at, client_ip, action, result) VALUES (?, ?, ?, ?)",
                (now, client_ip, action.value, result.value),
            )
        logger.info("audit: %s %s from %s", action.value, result.value, client_ip or "-")

    def recent(self, limit: int = 50) -> list[AuditEntry]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT at, client_ip, action, result FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            AuditEntry(datetime.fromtimestamp(float(r[0]), UTC), r[1], str(r[2]), str(r[3]))
            for r in rows
        ]

    def purge(self) -> None:
        """180 Tage und höchstens 50.000 Einträge."""
        with self._db.connect() as connection:
            connection.execute(
                "DELETE FROM audit_log WHERE at < ?", (self._clock() - RETENTION_SECONDS,)
            )
            connection.execute(
                "DELETE FROM audit_log WHERE id <= ("
                "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (MAX_ENTRIES,),
            )
