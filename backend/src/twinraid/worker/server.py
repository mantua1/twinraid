"""Unix-Socket des Workers (Konzept 3.2 und 5.4).

Schutzschichten:
1. Dateirechte: Verzeichnis 0750, Socket 0660, Gruppe des Web-Prozesses.
2. SO_PEERCRED: Nur die UID des Web-Prozesses darf Befehle senden.
3. Protokoll: feste Befehlsliste, keine freien Parameter, Größen- und Zeitgrenzen.
"""

import asyncio
import contextlib
import logging
import socket
import stat
import struct
from pathlib import Path

from twinraid.shared.ipc import (
    MAX_REQUEST_BYTES,
    ErrorCode,
    ErrorResponse,
    ProtocolError,
    Response,
    decode_command,
    encode_response,
)
from twinraid.worker.handler import CommandHandler

logger = logging.getLogger(__name__)

SOCKET_DIR_MODE = 0o750
SOCKET_MODE = 0o660
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
_PEERCRED_FORMAT = "3i"


class SocketPathError(Exception):
    """Der Socket-Pfad kann nicht sicher verwendet werden."""


def peer_uid(sock: socket.socket) -> int:
    """UID des Prozesses am anderen Ende, vom Kernel bestätigt."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_PEERCRED_FORMAT))
    _pid, uid, _gid = struct.unpack(_PEERCRED_FORMAT, raw)
    return int(uid)


def prepare_socket_path(path: Path) -> None:
    """Legt das Socket-Verzeichnis an und entfernt einen alten Socket.

    Eine vorhandene Datei, die kein Socket ist, wird nie gelöscht.
    """
    directory = path.parent
    directory.mkdir(mode=SOCKET_DIR_MODE, parents=True, exist_ok=True)
    if not directory.is_dir() or directory.is_symlink():
        raise SocketPathError(f"{directory} is not a directory")
    directory.chmod(SOCKET_DIR_MODE)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise SocketPathError(f"{path} exists and is not a socket; refusing to remove it")
    path.unlink()


class WorkerServer:
    def __init__(
        self,
        path: Path,
        allowed_uid: int,
        handler: CommandHandler,
        read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        self._path = path
        self._allowed_uid = allowed_uid
        self._handler = handler
        self._read_timeout = read_timeout
        self._server: asyncio.Server | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def start(self) -> None:
        prepare_socket_path(self._path)
        # Bei umask 077 entsteht der Socket zunächst mit 0700 und wird erst
        # danach für die Gruppe freigegeben. Es gibt also kein offenes Zeitfenster.
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self._path), limit=MAX_REQUEST_BYTES
        )
        self._path.chmod(SOCKET_MODE)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # asyncio liefert hier einen TransportSocket, der getsockopt() anbietet.
            uid = peer_uid(writer.get_extra_info("socket"))
            if uid != self._allowed_uid:
                logger.warning("rejected worker connection from uid %d", uid)
                response: Response = ErrorResponse(
                    error=ErrorCode.FORBIDDEN, message="caller is not allowed"
                )
            else:
                response = await self._process(reader)
            writer.write(encode_response(response))
            await writer.drain()
        except ConnectionError:
            logger.debug("client disconnected early")
        except Exception:  # Verbindung darf den Worker nie beenden.
            logger.exception("unexpected error while handling a worker connection")
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _process(self, reader: asyncio.StreamReader) -> Response:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=self._read_timeout)
        except TimeoutError:
            return _invalid("request timed out")
        except ValueError:
            # readline() meldet eine überschrittene Größengrenze als ValueError.
            return _invalid("request too large")
        try:
            command = decode_command(line)
        except ProtocolError as exc:
            return _invalid(str(exc))
        try:
            return await self._handler.handle(command)
        except Exception:  # Fehler im Handler werden gemeldet, nicht weitergereicht.
            logger.exception("command %s failed", command.command)
            return ErrorResponse(error=ErrorCode.INTERNAL_ERROR, message="command failed")


def _invalid(message: str) -> ErrorResponse:
    return ErrorResponse(error=ErrorCode.INVALID_REQUEST, message=message)
