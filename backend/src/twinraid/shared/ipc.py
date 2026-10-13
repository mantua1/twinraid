"""Befehlsprotokoll zwischen Web-Prozess und Worker (Konzept 3.2).

Die Befehlsliste ist fest. Befehle haben keine frei wählbaren Parameter;
erlaubt sind nur IDs, die der Worker selbst vergeben hat (Job-IDs).

Übertragung: Unix-Stream-Socket, pro Verbindung genau eine Anfrage und eine
Antwort, jeweils als eine JSON-Zeile (UTF-8, abgeschlossen mit "\\n").
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, TypeAdapter, ValidationError

from twinraid.shared.base import StrictModel

# Obergrenzen schützen den Worker vor übergroßen Nachrichten.
MAX_REQUEST_BYTES = 4 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

# Job-IDs vergibt ausschließlich der Worker (uuid4 als 32 Hex-Zeichen).
JobId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class GetStatus(StrictModel):
    command: Literal["get_status"]


class WakeTarget(StrictModel):
    command: Literal["wake_target"]


class ShutdownTarget(StrictModel):
    command: Literal["shutdown_target"]


class VmStart(StrictModel):
    command: Literal["vm_start"]


class VmStop(StrictModel):
    command: Literal["vm_stop"]


class VmForceStop(StrictModel):
    command: Literal["vm_force_stop"]


class SyncStart(StrictModel):
    command: Literal["sync_start"]


class SyncCancel(StrictModel):
    command: Literal["sync_cancel"]
    job_id: JobId


class SyncConfirmDeletions(StrictModel):
    command: Literal["sync_confirm_deletions"]
    job_id: JobId


Command = Annotated[
    GetStatus
    | WakeTarget
    | ShutdownTarget
    | VmStart
    | VmStop
    | VmForceStop
    | SyncStart
    | SyncCancel
    | SyncConfirmDeletions,
    Field(discriminator="command"),
]

# Vollständige, feste Befehlsliste laut Konzept 3.2. Ein Test prüft, dass das
# Protokoll genau diese Befehle kennt.
COMMAND_NAMES: frozenset[str] = frozenset(
    {
        "get_status",
        "wake_target",
        "shutdown_target",
        "vm_start",
        "vm_stop",
        "vm_force_stop",
        "sync_start",
        "sync_cancel",
        "sync_confirm_deletions",
    }
)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    FORBIDDEN = "forbidden"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"


class OkResponse(StrictModel):
    ok: Literal[True] = True
    result: JsonValue = None


class ErrorResponse(StrictModel):
    ok: Literal[False] = False
    error: ErrorCode
    message: str


Response = Annotated[OkResponse | ErrorResponse, Field(discriminator="ok")]

_command_adapter: TypeAdapter[Command] = TypeAdapter(Command)
_response_adapter: TypeAdapter[Response] = TypeAdapter(Response)


class ProtocolError(Exception):
    """Eine Nachricht verletzt das Protokoll."""


def _decode_line(line: bytes, limit: int) -> bytes:
    if len(line) > limit:
        raise ProtocolError("message too large")
    if not line.endswith(b"\n"):
        raise ProtocolError("message must end with a newline")
    return line[:-1]


def encode_command(command: Command) -> bytes:
    return command.model_dump_json().encode("utf-8") + b"\n"


def decode_command(line: bytes) -> Command:
    payload = _decode_line(line, MAX_REQUEST_BYTES)
    try:
        return _command_adapter.validate_json(payload)
    except ValidationError as exc:
        raise ProtocolError("invalid command") from exc


def encode_response(response: Response) -> bytes:
    data = response.model_dump_json().encode("utf-8") + b"\n"
    if len(data) > MAX_RESPONSE_BYTES:
        raise ProtocolError("response too large")
    return data


def decode_response(line: bytes) -> Response:
    payload = _decode_line(line, MAX_RESPONSE_BYTES)
    try:
        return _response_adapter.validate_json(payload)
    except ValidationError as exc:
        raise ProtocolError("invalid response") from exc
