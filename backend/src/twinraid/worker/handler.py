"""Führt Befehle aus der festen Befehlsliste aus."""

from typing import Protocol

from twinraid.integrations.jobs import JobState
from twinraid.shared.actions import ActionBlocker, ActionName
from twinraid.shared.ipc import (
    SYNC_COMMANDS,
    Command,
    ErrorCode,
    ErrorResponse,
    GetStatus,
    OkResponse,
    Response,
    ShutdownTarget,
    ShutdownTargetForceVm,
    VerifyPassword,
    VmForceStop,
    VmStart,
    VmStop,
    WakeTarget,
)
from twinraid.shared.mode import RunMode
from twinraid.shared.status import ResourceStatus, StatusSnapshot
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.password_check import PasswordCheck


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...


class CommandHandler:
    def __init__(
        self,
        status: StatusSource,
        password_check: PasswordCheck,
        jobs: JobState,
        mode: RunMode,
        actions: ActionCoordinator,
    ) -> None:
        self._status = status
        self._password_check = password_check
        self._jobs = jobs
        self._mode = mode
        self._actions = actions

    async def handle(self, command: Command) -> Response:
        # Der Sync folgt in Phase 5. Im Modus real gesperrt, bevor irgendetwas
        # verarbeitet wird (Konzept 6.4).
        if command.command in SYNC_COMMANDS:
            if self._mode is RunMode.REAL:
                return ErrorResponse(
                    error=ErrorCode.NOT_AVAILABLE,
                    message=f"command {command.command!r} is not available in real mode yet",
                )
            return ErrorResponse(
                error=ErrorCode.NOT_IMPLEMENTED,
                message=f"command {command.command!r} is not available in this version",
            )
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
            ):
                return await self._action(ActionName(command.command))
        return ErrorResponse(  # pragma: no cover - alle Befehle sind oben abgedeckt
            error=ErrorCode.NOT_IMPLEMENTED, message="unknown command"
        )

    def snapshot(self) -> StatusSnapshot:
        status, actions = self._actions.snapshot(self._status.current())
        return StatusSnapshot.model_validate(
            {
                **status.model_dump(),
                "job_running": self._jobs.is_running(),
                "password_set": self._password_check.is_set(),
                "actions": actions,
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
