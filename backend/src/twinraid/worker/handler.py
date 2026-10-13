"""Führt Befehle aus der festen Befehlsliste aus."""

from typing import Protocol

from twinraid.integrations.jobs import JobState
from twinraid.shared.ipc import (
    ACTION_COMMANDS,
    Command,
    ErrorCode,
    ErrorResponse,
    GetStatus,
    OkResponse,
    Response,
    VerifyPassword,
)
from twinraid.shared.mode import REAL_ACTIONS_ENABLED, RunMode
from twinraid.shared.status import ResourceStatus, StatusSnapshot
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
    ) -> None:
        self._status = status
        self._password_check = password_check
        self._jobs = jobs
        self._mode = mode

    async def handle(self, command: Command) -> Response:
        # Im Modus real sind Aktionen gesperrt, bevor irgendetwas verarbeitet wird (Konzept 6.4).
        if (
            self._mode is RunMode.REAL
            and not REAL_ACTIONS_ENABLED
            and command.command in ACTION_COMMANDS
        ):
            return ErrorResponse(
                error=ErrorCode.NOT_AVAILABLE,
                message=f"command {command.command!r} is not available in real mode yet",
            )
        match command:
            case GetStatus():
                snapshot = StatusSnapshot.model_validate(
                    {
                        **self._status.current().model_dump(),
                        "job_running": self._jobs.is_running(),
                        "password_set": self._password_check.is_set(),
                    }
                )
                return OkResponse(result=snapshot.model_dump(mode="json"))
            case VerifyPassword(password=password):
                outcome = await self._password_check.verify(password)
                return OkResponse(result={"outcome": outcome.value})
            case _:
                # Die übrigen Befehle folgen ab Phase 4.
                return ErrorResponse(
                    error=ErrorCode.NOT_IMPLEMENTED,
                    message=f"command {command.command!r} is not available in this version",
                )
