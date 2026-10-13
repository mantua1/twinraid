"""Client für den Worker-Socket. Sendet nur Befehle aus der festen Liste."""

import asyncio
import contextlib
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue, ValidationError

from twinraid.shared.ipc import (
    MAX_RESPONSE_BYTES,
    Command,
    ErrorCode,
    ErrorResponse,
    GetStatus,
    ProtocolError,
    VerifyPassword,
    decode_response,
    encode_command,
)
from twinraid.shared.passwords import VerifyOutcome
from twinraid.shared.status import StatusSnapshot

DEFAULT_TIMEOUT_SECONDS = 5.0
# Der Worker drosselt Passwortprüfungen um bis zu 30 Sekunden (Konzept 3.2).
VERIFY_TIMEOUT_SECONDS = 60.0


class WorkerUnavailableError(Exception):
    """Der Worker ist nicht erreichbar oder antwortet nicht protokollgemäß."""


class WorkerCommandError(Exception):
    """Der Worker hat den Befehl abgelehnt."""

    def __init__(self, error: ErrorCode, message: str) -> None:
        super().__init__(f"{error.value}: {message}")
        self.error = error


class WorkerApi(Protocol):
    async def get_status(self) -> StatusSnapshot: ...

    async def verify_password(self, password: str) -> VerifyOutcome: ...


class WorkerClient:
    def __init__(self, socket_path: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._socket_path = socket_path
        self._timeout = timeout

    async def send(self, command: Command, time_limit: float | None = None) -> JsonValue:
        try:
            async with asyncio.timeout(time_limit or self._timeout):
                reader, writer = await asyncio.open_unix_connection(
                    str(self._socket_path), limit=MAX_RESPONSE_BYTES
                )
                try:
                    writer.write(encode_command(command))
                    await writer.drain()
                    line = await reader.readline()
                finally:
                    writer.close()
                    with contextlib.suppress(ConnectionError):
                        await writer.wait_closed()
        except (OSError, TimeoutError, ValueError) as exc:
            raise WorkerUnavailableError("worker is not reachable") from exc
        try:
            response = decode_response(line)
        except ProtocolError as exc:
            raise WorkerUnavailableError("worker sent an invalid response") from exc
        if isinstance(response, ErrorResponse):
            raise WorkerCommandError(response.error, response.message)
        return response.result

    async def get_status(self) -> StatusSnapshot:
        result = await self.send(GetStatus(command="get_status"))
        try:
            return StatusSnapshot.model_validate(result)
        except ValidationError as exc:
            raise WorkerUnavailableError("worker sent an invalid status") from exc

    async def verify_password(self, password: str) -> VerifyOutcome:
        command = VerifyPassword(command="verify_password", password=password)
        result = await self.send(command, time_limit=VERIFY_TIMEOUT_SECONDS)
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if not isinstance(outcome, str):
            raise WorkerUnavailableError("worker sent an invalid answer")
        try:
            return VerifyOutcome(outcome)
        except ValueError as exc:
            raise WorkerUnavailableError("worker sent an invalid answer") from exc
