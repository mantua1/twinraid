"""Führt Befehle aus der festen Befehlsliste aus."""

from typing import Protocol

from twinraid.integrations.jobs import JobState
from twinraid.integrations.source_guard import SourceGuardSource
from twinraid.shared.actions import ActionBlocker, ActionName
from twinraid.shared.ipc import (
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
from twinraid.shared.status import ResourceStatus, StatusSnapshot
from twinraid.shared.sync import SyncStatus
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.password_check import PasswordCheck


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...


class SyncSource(Protocol):
    """Probelauf (sync/coordinator.py, ab v0.5.1)."""

    def snapshot(self) -> SyncStatus: ...

    def cancel(self, job_id: str) -> bool: ...


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
    ) -> None:
        self._status = status
        self._password_check = password_check
        self._jobs = jobs
        self._mode = mode
        self._actions = actions
        self._source_guard = source_guard
        self._sync = sync

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
            ):
                return await self._action(ActionName(command.command))
            case SyncCancel(job_id=job_id):
                if self._sync.cancel(job_id):
                    return OkResponse(result={"accepted": True})
                return ErrorResponse(
                    error=ErrorCode.ACTION_BLOCKED, message="no dry run with this job id is running"
                )
            case SyncConfirmDeletions():
                # Ein Probelauf fragt nie nach; der echte Sync kommt mit v0.5.2 (Konzept 4.3).
                return ErrorResponse(
                    error=ErrorCode.NOT_AVAILABLE,
                    message="confirming deletions comes with the sync in v0.5.2",
                    blocked_by=ActionBlocker.NEXT_VERSION,
                )
        return ErrorResponse(  # pragma: no cover - alle Befehle sind oben abgedeckt
            error=ErrorCode.NOT_IMPLEMENTED, message="unknown command"
        )

    def snapshot(self) -> StatusSnapshot:
        status, actions = self._actions.snapshot(self._status.current())
        # Der Wächter wird erst hier ausgewertet, damit das Alter des Herzschlags stimmt.
        guard = self._source_guard.report()
        return StatusSnapshot.model_validate(
            {
                **status.model_dump(),
                "notices": (*status.notices, *guard.notices),
                "job_running": self._jobs.is_running(),
                "password_set": self._password_check.is_set(),
                "actions": actions,
                "source_guard": guard.status,
                "sync": self._sync.snapshot(),
            }
        )

    async def _action(self, name: ActionName) -> Response:
        blocker = await self._actions.request(name)
        if blocker is None:
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
