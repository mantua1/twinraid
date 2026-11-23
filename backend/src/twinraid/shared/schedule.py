"""Zeitplan und automatisches Herunterfahren in get_status (Konzept 4.2 und 4.8, ab v0.6.0).

Beide Felder liegen nur zwischen Worker, Web-Prozess und Oberfläche; alle Codes stammen aus
festen Listen, die Werte des Zeitplans aus der geprüften Konfiguration.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from twinraid.shared.actions import ActionBlocker, ActionOutcome
from twinraid.shared.base import StrictModel
from twinraid.shared.sync import SyncReason

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ScheduleLimit(StrEnum):
    """Grenze von Wächter oder Groß, die so lang ist wie der kürzeste Abstand zwischen zwei
    Terminen oder länger (Konzept 4.8, „Takt gegen die Grenzen des Wächters“)."""

    MIN_RUN_INTERVAL = "min_run_interval_minutes"
    MIN_SNAPSHOT_INTERVAL = "min_snapshot_interval_minutes"
    # min_interval_minutes aus mirror.conf auf Groß, laut letztem check.
    MIN_RECEIVE_INTERVAL = "min_interval_minutes"


class ScheduleConflict(StrictModel):
    limit: ScheduleLimit
    minutes: int


class ScheduleWaiting(StrictModel):
    """Ein Termin wartet auf eine Sperre (Konzept 4.8, „Sperre zum Termin“)."""

    term: datetime
    reason: ActionBlocker
    next_attempt_at: datetime
    # Höchstens 60 Minuten nach dem ersten Versuch; danach schedule_skipped.
    until: datetime


class ScheduleLast(StrictModel):
    """Letzter geplanter Lauf aus der Sync-Historie."""

    scheduled_for: datetime
    outcome: ActionOutcome
    reason: SyncReason | None
    finished_at: datetime


class ScheduleStatus(StrictModel):
    enabled: bool
    # Im Modus real braucht der Zeitplan actions.sync (Konzept 4.2); sonst „nicht freigeschaltet“.
    permitted: bool
    timezone: str
    days: tuple[Weekday, ...]
    time: str
    # Groß wird geweckt, wenn es aus ist (wake_target und Freischaltung), und danach
    # heruntergefahren, wenn der Lauf geweckt hat (shutdown_after und Freischaltung).
    wake: bool
    shutdown: bool
    shutdown_delay_minutes: int
    # Herunterfahren nach dem Spielen (gaming_vm.shutdown_target_after mit Freischaltung).
    play_shutdown: bool
    play_delay_minutes: int
    # Die nächsten drei Termine in UTC; leer, wenn der Zeitplan aus ist.
    next_terms: tuple[datetime, ...]
    waiting: ScheduleWaiting | None = None
    last: ScheduleLast | None = None
    # Kürzester Abstand zwischen zwei Terminen in Minuten und die Grenzen, die ihn erreichen.
    shortest_gap_minutes: int | None = None
    conflicts: tuple[ScheduleConflict, ...] = ()


class AutoShutdownKind(StrEnum):
    """Art der Vormerkung (Konzept 4.2, „Automatisches Herunterfahren“)."""

    AFTER_SYNC = "after_sync"
    AFTER_PLAY = "after_play"


class AutoShutdownReason(StrEnum):
    """Warum die Wartezeit gerade nicht läuft."""

    VM_RUNNING = "vm_running"
    VM_UNKNOWN = "vm_unknown"
    OTHER_VMS_RUNNING = "other_vms_running"
    BUSY = "busy"
    SYNC_RUNNING = "sync_running"
    NOT_ENABLED = "not_enabled"


class AutoShutdownStatus(StrictModel):
    """Vorgemerktes Herunterfahren: Zeitpunkt, solange alle Bedingungen gelten, sonst der Grund."""

    kinds: tuple[AutoShutdownKind, ...]
    at: datetime | None
    reason: AutoShutdownReason | None
