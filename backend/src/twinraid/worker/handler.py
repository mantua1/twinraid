"""Führt Befehle aus der festen Befehlsliste aus."""

from collections.abc import Callable
from typing import Protocol

from twinraid.integrations.jobs import JobState
from twinraid.integrations.source_guard import SourceGuardSource
from twinraid.shared.actions import ActionBlocker, ActionName
from twinraid.shared.ipc import (
    AutoShutdownCancel,
    Command,
    ErrorCode,
    ErrorResponse,
    GetStatus,
    OkResponse,
    Response,
    ShutdownTarget,
    ShutdownTargetForceVm,
    SyncCancel,
    SyncConfirmDeletions,
    SyncDryRun,
    SyncStart,
    VerifyPassword,
    VmForceStop,
    VmStart,
    VmStop,
    WakeTarget,
)
from twinraid.shared.mode import RunMode
from twinraid.shared.schedule import AutoShutdownStatus, ScheduleStatus
from twinraid.shared.status import ResourceStatus, StatusSnapshot
from twinraid.shared.sync import SyncStatus
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.password_check import PasswordCheck


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...


class SyncSource(Protocol):
    """Probelauf und Sync (sync/coordinator.py, ab v0.5.1 bzw. v0.5.2)."""

    def snapshot(self) -> SyncStatus: ...

    def cancel(self, job_id: str) -> bool: ...

    def confirm(self, job_id: str) -> bool: ...


class ScheduleSource(Protocol):
    """Zeitplan (worker/scheduler.py, ab v0.6.0)."""

    def snapshot(self) -> ScheduleStatus: ...


class AutoShutdownSource(Protocol):
    """Automatisches Herunterfahren (worker/auto_shutdown.py, ab v0.6.0)."""

    def snapshot(self, status: ResourceStatus) -> AutoShutdownStatus | None: ...

    def on_manual(self, command: str) -> None: ...


# Rundlauf der Simulation: ersetzt Zeitplan und Vormerkung für die Anzeige (Konzept 6.4).
TourOverlay = Callable[
    [ScheduleStatus, AutoShutdownStatus | None],
    tuple[ScheduleStatus, AutoShutdownStatus | None],
]


def _no_tour(
    schedule: ScheduleStatus, auto: AutoShutdownStatus | None
) -> tuple[ScheduleStatus, AutoShutdownStatus | None]:
    return schedule, auto


class CommandHandler:
    def __init__(
        self,
        status: StatusSource,
        password_check: PasswordCheck,
        jobs: JobState,
        mode: RunMode,
        actions: ActionCoordinator,
        source_guard: SourceGuardSource,
        sync: SyncSource,
        schedule: ScheduleSource,
        auto_shutdown: AutoShutdownSource,
        tour: TourOverlay = _no_tour,
    ) -> None:
        self._status = status
        self._password_check = password_check
        self._jobs = jobs
        self._mode = mode
        self._actions = actions
        self._source_guard = source_guard
        self._sync = sync
        self._schedule = schedule
        self._auto_shutdown = auto_shutdown
        self._tour = tour

    async def handle(self, command: Command) -> Response:
        match command:
            case GetStatus():
                return OkResponse(result=self.snapshot().model_dump(mode="json"))
            case VerifyPassword(password=password):
                outcome = await self._password_check.verify(password)
                return OkResponse(result={"outcome": outcome.value})
            case (
                WakeTarget()
                | ShutdownTarget()
                | ShutdownTargetForceVm()
                | VmStart()
                | VmStop()
                | VmForceStop()
                | SyncDryRun()
                | SyncStart()
                | AutoShutdownCancel()
            ):
                return await self._action(ActionName(command.command))
            case SyncCancel(job_id=job_id):
                if self._sync.cancel(job_id):
                    self._auto_shutdown.on_manual(command.command)
                    return OkResponse(result={"accepted": True})
                return ErrorResponse(
                    error=ErrorCode.ACTION_BLOCKED, message="no run with this job id is running"
                )
            case SyncConfirmDeletions(job_id=job_id):
                if self._sync.confirm(job_id):
                    self._auto_shutdown.on_manual(command.command)
                    return OkResponse(result={"accepted": True})
                return ErrorResponse(
                    error=ErrorCode.ACTION_BLOCKED,
                    message="no sync with this job id is waiting for a confirmation",
                )
        return ErrorResponse(  # pragma: no cover - alle Befehle sind oben abgedeckt
            error=ErrorCode.NOT_IMPLEMENTED, message="unknown command"
        )

    def snapshot(self) -> StatusSnapshot:
        current = self._status.current()
        status, actions = self._actions.snapshot(current)
        # Der Wächter wird erst hier ausgewertet, damit das Alter des Herzschlags stimmt.
        guard = self._source_guard.report()
        schedule, auto = self._tour(
            self._schedule.snapshot(), self._auto_shutdown.snapshot(current)
        )
        return StatusSnapshot.model_validate(
            {
                **status.model_dump(),
                "notices": (*status.notices, *guard.notices),
                "job_running": self._jobs.is_running(),
                "password_set": self._password_check.is_set(),
                "actions": actions,
                "source_guard": guard.status,
                "sync": self._sync.snapshot(),
                "schedule": schedule,
                "auto_shutdown": auto,
            }
        )

    async def _action(self, name: ActionName) -> Response:
        blocker = await self._actions.request(name)
        if blocker is None:
            # Eine manuelle Aktion löscht die passende Vormerkung (Konzept 4.2).
            self._auto_shutdown.on_manual(name.value)
            return OkResponse(result={"accepted": True})
        if blocker is ActionBlocker.NOT_ENABLED:
            # Im Modus real nicht in actions.* freigeschaltet (Konzept 4.2 und 6.4).
            return ErrorResponse(
                error=ErrorCode.NOT_AVAILABLE,
                message=f"command {name.value!r} is not enabled in real mode",
                blocked_by=blocker,
            )
        return ErrorResponse(
            error=ErrorCode.ACTION_BLOCKED,
            message=f"command {name.value!r} is not possible right now",
            blocked_by=blocker,
        )
