"""Automatisches Herunterfahren von Groß (Konzept 4.2, Entscheidung E30, ab v0.6.0).

Zwei Vormerkungen, nur im Speicher des Workers:
- nach dem geplanten Sync: ein geplanter Lauf hat Groß selbst geweckt, shutdown_after gilt;
- nach dem Spielen: „VM starten“ hat Groß automatisch geweckt, shutdown_target_after gilt.

Groß wird erst heruntergefahren, wenn alle Bedingungen die ganze Wartezeit lang gelten; fehlt
eine zwischendurch, beginnt die Wartezeit neu. Ausgeführt wird derselbe Ablauf wie
„Groß herunterfahren“ mit allen Prüfungen; scheitert er, gibt es keinen zweiten Versuch.
Ein Neustart des Workers löscht beide Vormerkungen (sicherer Ausfall: Groß bleibt an).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.shared.actions import ActionName
from twinraid.shared.mode import RunMode
from twinraid.shared.schedule import AutoShutdownKind, AutoShutdownReason, AutoShutdownStatus
from twinraid.shared.status import ResourceState, ResourceStatus

logger = logging.getLogger(__name__)

TICK_SECONDS = 10.0

# Welche Anforderung aus der Oberfläche welche Vormerkung löscht (Konzept 4.2, „Manuelle
# Aktion“). VM starten, herunterfahren und erzwingen gehören zum Spielen.
_CLEARS_AFTER_SYNC = frozenset(
    {
        "vm_start",
        "vm_stop",
        "vm_force_stop",
        "shutdown_target",
        "shutdown_target_force_vm",
        "sync_dry_run",
        "sync_start",
        "sync_cancel",
        "sync_confirm_deletions",
    }
)
_CLEARS_AFTER_PLAY = frozenset(
    {"sync_dry_run", "sync_start", "sync_cancel", "sync_confirm_deletions"}
)
# Die Aktion, die Groß geweckt hat (part_of im Aktionslog).
_PART_OF = {
    AutoShutdownKind.AFTER_SYNC: ActionName.SYNC_START,
    AutoShutdownKind.AFTER_PLAY: ActionName.VM_START,
}


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...


class ShutdownActions(Protocol):
    """Was das Herunterfahren vom ActionCoordinator braucht."""

    @property
    def busy(self) -> bool: ...

    def start_automatic_shutdown(self, part_of: ActionName) -> bool: ...


class SyncState(Protocol):
    def is_running(self) -> bool: ...


@dataclass(frozen=True)
class _Mark:
    kind: AutoShutdownKind
    set_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AutoShutdown:
    def __init__(
        self,
        config: TwinRaidConfig,
        mode: RunMode,
        status: StatusSource,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self._config = config
        self._mode = mode
        self._status = status
        self._clock = clock
        self._sleep = sleep
        self._tick_seconds = tick_seconds
        self._marks: dict[AutoShutdownKind, _Mark] = {}
        # Seit wann alle Bedingungen ohne Unterbrechung gelten.
        self._since: datetime | None = None
        self._actions: ShutdownActions | None = None
        self._sync: SyncState | None = None

    def attach(self, actions: ShutdownActions, sync: SyncState) -> None:
        self._actions = actions
        self._sync = sync

    # --- Vormerkungen ---

    def mark_after_sync(self) -> None:
        """Ein geplanter Lauf hat Groß geweckt (Teilschritt wake_target)."""
        if self._config.sync.schedule.shutdown_after:
            self._mark(AutoShutdownKind.AFTER_SYNC)

    def mark_after_play(self) -> None:
        """„VM starten“ hat Groß automatisch geweckt."""
        if self._config.gaming_vm.shutdown_target_after.enabled:
            self._mark(AutoShutdownKind.AFTER_PLAY)

    def _mark(self, kind: AutoShutdownKind) -> None:
        if not self._enabled():
            # Ohne actions.shutdown zeigt die Kachel Groß kein Herunterfahren (Konzept 4.2).
            logger.info("automatic shutdown not planned (%s): actions.shutdown is off", kind.value)
            return
        self._marks[kind] = _Mark(kind, self._clock())
        self._since = None
        logger.info("automatic shutdown of the target planned: %s", kind.value)

    def on_manual(self, command: str) -> None:
        """Eine Anforderung aus der Oberfläche, die der Worker angenommen hat."""
        cleared = []
        if command in _CLEARS_AFTER_SYNC and AutoShutdownKind.AFTER_SYNC in self._marks:
            cleared.append(AutoShutdownKind.AFTER_SYNC)
        if command in _CLEARS_AFTER_PLAY and AutoShutdownKind.AFTER_PLAY in self._marks:
            cleared.append(AutoShutdownKind.AFTER_PLAY)
        for kind in cleared:
            del self._marks[kind]
            logger.info("automatic shutdown dropped (%s): manual action %s", kind.value, command)
        if cleared:
            self._since = None

    def offered(self) -> bool:
        """„Groß anlassen“ ist nur möglich, solange eine Vormerkung besteht."""
        return bool(self._marks)

    def cancel(self) -> bool:
        """„Groß anlassen“: löscht beide Vormerkungen. False, wenn keine bestand."""
        if not self._marks:
            return False
        self._clear("keep the target on")
        return True

    def _clear(self, why: str) -> None:
        kinds = ", ".join(kind.value for kind in self._marks)
        self._marks.clear()
        self._since = None
        logger.info("automatic shutdown dropped (%s): %s", kinds, why)

    # --- Bedingungen ---

    def _enabled(self) -> bool:
        return self._mode is RunMode.MOCK or self._config.actions.shutdown

    def _delay(self) -> timedelta:
        """Sind beide Vormerkungen gesetzt, gilt die längere Wartezeit."""
        minutes = 0
        if AutoShutdownKind.AFTER_SYNC in self._marks:
            minutes = self._config.sync.schedule.shutdown_delay_minutes
        if AutoShutdownKind.AFTER_PLAY in self._marks:
            delay = self._config.gaming_vm.shutdown_target_after.delay_minutes
            minutes = max(minutes, delay)
        return timedelta(minutes=minutes)

    def reason(self, status: ResourceStatus) -> AutoShutdownReason | None:
        """Warum Groß gerade nicht heruntergefahren würde; None: alle Bedingungen gelten."""
        if not self._enabled():
            return AutoShutdownReason.NOT_ENABLED
        if self._sync is not None and self._sync.is_running():
            return AutoShutdownReason.SYNC_RUNNING
        if self._actions is None or self._actions.busy:
            return AutoShutdownReason.BUSY
        if status.target.state is not ResourceState.ONLINE:
            return AutoShutdownReason.VM_UNKNOWN
        vm = status.vm
        if vm.state is ResourceState.UNKNOWN:
            return AutoShutdownReason.VM_UNKNOWN
        # Nur SHUTOFF zählt als aus: Offline ohne Ursache. CRASHED, PAUSED und PMSUSPENDED nicht.
        if vm.state is not ResourceState.OFFLINE or vm.reason is not None:
            return AutoShutdownReason.VM_RUNNING
        if status.other_vms_running is None:
            return AutoShutdownReason.VM_UNKNOWN
        if status.other_vms_running:
            return AutoShutdownReason.OTHER_VMS_RUNNING
        return None

    # --- Ablauf ---

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # Darf den Worker nie beenden.
                logger.exception("automatic shutdown check failed")
            await self._sleep(self._tick_seconds)

    def tick(self) -> None:
        if not self._marks:
            self._since = None
            return
        status = self._status.current()
        now = self._clock()
        newest = max(mark.set_at for mark in self._marks.values())
        checked = status.checked_at
        if (
            status.target.state is ResourceState.OFFLINE
            and checked is not None
            and checked > newest
        ):
            # Groß ist aus: Es gibt nichts mehr herunterzufahren.
            self._clear("the target is offline")
            return
        if self.reason(status) is not None:
            self._since = None
            return
        if self._since is None:
            self._since = now
            return
        if now < self._since + self._delay():
            return
        kinds = sorted(self._marks, key=lambda kind: self._marks[kind].set_at)
        part_of = _PART_OF[kinds[0]]
        self._clear("shutting down the target now")
        if self._actions is not None and not self._actions.start_automatic_shutdown(part_of):
            logger.warning("automatic shutdown could not start: another action is running")

    def snapshot(self, status: ResourceStatus) -> AutoShutdownStatus | None:
        if not self._marks:
            return None
        kinds = tuple(sorted(self._marks, key=lambda kind: self._marks[kind].set_at))
        reason = self.reason(status)
        if reason is not None:
            return AutoShutdownStatus(kinds=kinds, at=None, reason=reason)
        since = self._since or self._clock()
        return AutoShutdownStatus(kinds=kinds, at=since + self._delay(), reason=None)
