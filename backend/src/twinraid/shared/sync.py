"""Sync-Teil von get_status: Probelauf, Ergebnis und Bericht (Konzept 4.3, ab v0.5.1).

Alle Codes stammen aus festen Listen; die Werte je Share übernimmt der Worker nur aus
dem streng geprüften Status des Wächters. Share-Namen kommen aus der Konfiguration bzw.
vom Wächter und erscheinen nur über geschützte Routen.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from twinraid.shared.actions import ActionOutcome
from twinraid.shared.base import StrictModel
from twinraid.shared.ipc import JobId
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheck,
    GuardCheckResult,
    GuardConfirmReason,
    GuardDatasetProblem,
    GuardOrderResult,
)


class SyncKind(StrEnum):
    DRY_RUN = "dry_run"


class SyncStep(StrEnum):
    """Schritt eines laufenden Probelaufs (Konzept 4.3, „Probelauf in TwinRaid“)."""

    WAKING = "waking"
    WAITING_FOR_VM_SERVICE = "waiting_for_vm_service"
    SNAPSHOT = "snapshot"
    PREPARING = "preparing"
    CLOSING = "closing"


# Grund zu einem Ergebnis: bei target_refused ein Eintrag aus blockers, bei
# target_check_failed das Ergebnis von check, bei source_guard_refused das Ergebnis des
# Auftrags. Alle drei Listen stammen aus source-guard.sh.
SyncReason = GuardBlocker | GuardCheckResult | GuardOrderResult


class SyncRun(StrictModel):
    kind: SyncKind
    job_id: JobId
    step: SyncStep
    started_at: datetime


class SyncResult(StrictModel):
    kind: SyncKind
    outcome: ActionOutcome
    reason: SyncReason | None = None
    finished_at: datetime


class SyncMirror(StrictModel):
    """Spiegel auf Groß laut check (Konzept 3.7)."""

    state: Literal["absent", "mirror", "foreign"]
    props: Literal["ok", "mismatch"] | None
    count: int | None
    newest_received: datetime | None


class SyncShareReport(StrictModel):
    """Ergebnis von prepare je Share."""

    name: str
    mode: Literal["full", "incremental"] | None
    base_type: Literal["snapshot", "bookmark"] | None
    problem: GuardDatasetProblem | None
    deleted: int | None
    modified: int | None
    added: int | None
    renamed: int | None
    referenced: int | None
    base_referenced: int | None
    size: int | None
    # None: Groß hat check nicht beantwortet oder kennt den Namen nicht (mirror.conf).
    mirror: SyncMirror | None


class SyncReport(StrictModel):
    """Bericht des letzten Probelaufs; der Worker behält ihn nur im Speicher."""

    prepared_at: datetime
    snapshot: str
    awaiting_confirm: bool
    confirm_reasons: tuple[GuardConfirmReason, ...]
    send_ready: bool
    blockers: tuple[GuardBlocker, ...]
    check: GuardCheck | None
    shares: tuple[SyncShareReport, ...]


class SyncLevels(StrictModel):
    """Die drei Ebenen (Konzept 4.3): twinraid.yaml, source-guard.conf, mirror.conf."""

    configured: tuple[str, ...]
    # None: unbekannt (Wächter ohne frischen Status bzw. noch kein check).
    source: tuple[str, ...] | None
    target: tuple[str, ...] | None
    # Zeitpunkt des check, aus dem target stammt.
    target_checked_at: datetime | None


class SyncStatus(StrictModel):
    running: SyncRun | None = None
    last: SyncResult | None = None
    report: SyncReport | None = None
    levels: SyncLevels
