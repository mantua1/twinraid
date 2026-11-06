"""Sync-Teil von get_status: laufender Lauf, Ergebnis, Bericht und Historie (Konzept 4.3).

Alle Codes stammen aus festen Listen; die Werte je Share übernimmt der Worker nur aus
dem streng geprüften Status des Wächters. Share-Namen kommen aus der Konfiguration bzw.
vom Wächter und erscheinen nur über geschützte Routen.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from twinraid.shared.actions import ActionOutcome, Trigger
from twinraid.shared.base import StrictModel
from twinraid.shared.ipc import JobId
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheck,
    GuardCheckResult,
    GuardConfirmReason,
    GuardDatasetProblem,
    GuardDatasetResult,
    GuardJobReason,
    GuardOrderResult,
)


class SyncKind(StrEnum):
    DRY_RUN = "dry_run"
    # Echter Sync ab v0.5.2.
    SYNC = "sync"


class SyncStep(StrEnum):
    """Schritt eines laufenden Laufs (Konzept 4.3)."""

    WAKING = "waking"
    WAITING_FOR_VM_SERVICE = "waiting_for_vm_service"
    # Nur der Sync (ab v0.5.2): Container anhalten bzw. wieder starten.
    STOPPING_CONTAINERS = "stopping_containers"
    SNAPSHOT = "snapshot"
    STARTING_CONTAINERS = "starting_containers"
    PREPARING = "preparing"
    # Nur der Sync: Warten auf die Freigabe von Löschungen, dann Senden.
    AWAITING_CONFIRM = "awaiting_confirm"
    SENDING = "sending"
    CLOSING = "closing"


# Grund zu einem Ergebnis: bei target_refused ein Eintrag aus blockers oder der Grund aus
# dem Job, bei target_check_failed das Ergebnis von check, bei source_guard_refused das
# Ergebnis des Auftrags, bei send_failed der Grund aus dem Job. Alle Listen stammen aus
# source-guard.sh.
SyncReason = GuardBlocker | GuardCheckResult | GuardOrderResult | GuardJobReason


class SyncConfirmation(StrictModel):
    """Freigabe von Löschungen, auf die der Sync wartet (Konzept 4.3, Schritt 5)."""

    reasons: tuple[GuardConfirmReason, ...]
    # Summe der Löschungen über alle Shares und die Grenze aus source-guard.conf.
    deletions: int
    max_deletions: int
    # Danach schließt der Wächter den Job (job.deadline); None: unbekannt.
    deadline: datetime | None


class SyncProgress(StrictModel):
    """Fortschritt beim Senden (Konzept 4.4)."""

    # Share, die gerade gesendet wird; None, wenn keine mehr offen ist.
    share: str | None
    sent: int
    total: int
    # Höchstens 99, bis der Job fertig ist, weil die Größe aus prepare geschätzt ist.
    percent: int
    # Erst wenn Werte über 15 s vorliegen; Bytes je Sekunde.
    rate: float | None
    remaining_seconds: int | None
    finish_at: datetime | None


class SyncRun(StrictModel):
    kind: SyncKind
    job_id: JobId
    step: SyncStep
    started_at: datetime
    # Nach einem Neustart von TwinRaid übernommene Übertragung (ab v0.5.2).
    adopted: bool = False
    containers_stopped: int = 0
    confirmation: SyncConfirmation | None = None
    progress: SyncProgress | None = None


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
    """Ergebnis von prepare je Share; nach dem Senden auch gesendete Bytes und Ergebnis."""

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
    sent: int | None = None
    result: GuardDatasetResult | None = None


class SyncReport(StrictModel):
    """Bericht des letzten Laufs; der Worker behält ihn nur im Speicher."""

    kind: SyncKind = SyncKind.DRY_RUN
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


class SyncHistoryShare(StrictModel):
    name: str
    mode: Literal["full", "incremental"] | None
    deleted: int | None
    modified: int | None
    added: int | None
    renamed: int | None
    size: int | None
    sent: int | None
    result: GuardDatasetResult | None


class SyncHistoryEntry(StrictModel):
    """Eine Zeile der Sync-Historie (Konzept 5.3); keine Snapshot- und Container-Namen."""

    id: str
    kind: SyncKind
    trigger: Trigger
    started_at: datetime
    finished_at: datetime
    result: ActionOutcome
    reason: SyncReason | None
    woke_target: bool
    containers_stopped: int
    adopted: bool
    shares: tuple[SyncHistoryShare, ...]


class SyncStatus(StrictModel):
    running: SyncRun | None = None
    last: SyncResult | None = None
    report: SyncReport | None = None
    levels: SyncLevels
    # Die letzten 20 Läufe aus der Sync-Historie, neueste zuerst (ab v0.5.2).
    history: tuple[SyncHistoryEntry, ...] = ()
