"""Führt Befehle aus der festen Befehlsliste aus."""

from twinraid.integrations.status import StatusProvider
from twinraid.shared.ipc import Command, ErrorCode, ErrorResponse, GetStatus, OkResponse, Response


class CommandHandler:
    def __init__(self, status_provider: StatusProvider) -> None:
        self._status_provider = status_provider

    async def handle(self, command: Command) -> Response:
        match command:
            case GetStatus():
                snapshot = await self._status_provider.get_status()
                return OkResponse(result=snapshot.model_dump(mode="json"))
            case _:
                # Die übrigen Befehle folgen ab Phase 4.
                return ErrorResponse(
                    error=ErrorCode.NOT_IMPLEMENTED,
                    message=f"command {command.command!r} is not available in this version",
                )
