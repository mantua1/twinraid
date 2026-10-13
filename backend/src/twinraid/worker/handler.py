"""Führt Befehle aus der festen Befehlsliste aus."""

from twinraid.integrations.status import StatusProvider
from twinraid.shared.ipc import (
    Command,
    ErrorCode,
    ErrorResponse,
    GetStatus,
    OkResponse,
    Response,
    VerifyPassword,
)
from twinraid.shared.status import StatusSnapshot
from twinraid.worker.password_check import PasswordCheck


class CommandHandler:
    def __init__(self, status_provider: StatusProvider, password_check: PasswordCheck) -> None:
        self._status_provider = status_provider
        self._password_check = password_check

    async def handle(self, command: Command) -> Response:
        match command:
            case GetStatus():
                resources = await self._status_provider.get_status()
                snapshot = StatusSnapshot(
                    **resources.model_dump(), password_set=self._password_check.is_set()
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
