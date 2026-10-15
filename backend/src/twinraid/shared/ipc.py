"""Befehlsprotokoll zwischen Web-Prozess und Worker (Konzept 3.2).

Die Befehlsliste ist fest. Befehle haben keine frei wählbaren Parameter;
erlaubt sind nur IDs, die der Worker selbst vergeben hat (Job-IDs). Einzige
Ausnahme ist das Passwort bei verify_password, das nur verglichen wird.

Übertragung: Unix-Stream-Socket, pro Verbindung genau eine Anfrage und eine
Antwort, jeweils als eine JSON-Zeile (UTF-8, abgeschlossen mit "\\n").
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, TypeAdapter, ValidationError

from twinraid.shared.actions import ActionBlocker
from twinraid.shared.base import StrictModel

# Obergrenzen schützen den Worker vor übergroßen Nachrichten. Ein Passwort mit
# 1024 Zeichen braucht als JSON mit Escapes höchstens 6 KiB.
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

# Job-IDs vergibt ausschließlich der Worker (uuid4 als 32 Hex-Zeichen).
JobId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class GetStatus(StrictModel):
    command: Literal["get_status"]


class VerifyPassword(StrictModel):
    command: Literal["verify_password"]
    # repr=False: Das Passwort darf nie in Logs oder Fehlermeldungen landen.
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)] = Field(repr=False)


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
    | VerifyPassword
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
        "verify_password",
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


# Befehle mit Außenwirkung (Konzept 4.2 und 6.4).
ACTION_COMMANDS: frozenset[str] = COMMAND_NAMES - {"get_status", "verify_password"}
# Der Sync folgt in Phase 5; bis dahin sind diese Befehle gesperrt.
SYNC_COMMANDS: frozenset[str] = frozenset({"sync_start", "sync_cancel", "sync_confirm_deletions"})


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    FORBIDDEN = "forbidden"
    NOT_IMPLEMENTED = "not_implemented"
    # Im Modus real nicht freigeschaltet („nicht verfügbar“).
    NOT_AVAILABLE = "not_available"
    # Die Aktion ist gerade nicht möglich; blocked_by nennt den Grund.
    ACTION_BLOCKED = "action_blocked"
    INTERNAL_ERROR = "internal_error"


class OkResponse(StrictModel):
    ok: Literal[True] = True
    result: JsonValue = None


class ErrorResponse(StrictModel):
    ok: Literal[False] = False
    error: ErrorCode
    message: str
    # Bei abgelehnten Aktionen der Grund aus der festen Liste (Konzept 4.2).
    blocked_by: ActionBlocker | None = None


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
