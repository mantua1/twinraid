"""Worker-Socket: Rechte, Aufruferprüfung und Protokoll (Konzept 3.2 und 5.4)."""

import asyncio
import json
import os
import shutil
import socket
import stat
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from tests.support.passwords import TEST_PASSWORD, password_check
from twinraid.integrations.mocks.status import MockStatusProvider
from twinraid.shared.ipc import MAX_REQUEST_BYTES, ErrorCode, ErrorResponse, OkResponse, Response
from twinraid.shared.ipc import decode_response as decode
from twinraid.worker.handler import CommandHandler
from twinraid.worker.server import SocketPathError, WorkerServer, prepare_socket_path

pytestmark = pytest.mark.anyio

JOB_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    # Unix-Socket-Pfade sind auf 108 Byte begrenzt; daher ein kurzer Pfad.
    base = Path(tempfile.mkdtemp(prefix="tr-"))
    try:
        yield base / "run"
    finally:
        shutil.rmtree(base)


async def _start(path: Path, allowed_uid: int, read_timeout: float = 2.0) -> WorkerServer:
    server = WorkerServer(
        path,
        allowed_uid,
        CommandHandler(MockStatusProvider(), password_check(path.parent.parent)),
        read_timeout=read_timeout,
    )
    await server.start()
    return server


@pytest.fixture
async def server(socket_dir: Path) -> AsyncIterator[WorkerServer]:
    worker = await _start(socket_dir / "worker.sock", os.getuid())
    try:
        yield worker
    finally:
        await worker.close()


async def _raw(path: Path, payload: bytes) -> Response:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(payload)
        await writer.drain()
        if payload and not payload.endswith(b"\n"):
            writer.write_eof()
        line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    return decode(line)


async def _error_code(path: Path, payload: bytes) -> ErrorCode:
    response = await _raw(path, payload)
    assert isinstance(response, ErrorResponse)
    return response.error


async def test_get_status_returns_unknown(server: WorkerServer) -> None:
    response = await _raw(server.path, b'{"command":"get_status"}\n')
    assert isinstance(response, OkResponse)
    assert response.result == {
        "source": "unknown",
        "target": "unknown",
        "vm": "unknown",
        "checked_at": None,
        "password_set": True,
    }


@pytest.mark.parametrize(
    ("password", "outcome"),
    [(TEST_PASSWORD, "valid"), ("falsches-passwort-123", "invalid"), ("x", "invalid")],
)
async def test_verify_password(server: WorkerServer, password: str, outcome: str) -> None:
    payload = json.dumps({"command": "verify_password", "password": password}).encode() + b"\n"
    response = await _raw(server.path, payload)
    assert isinstance(response, OkResponse)
    assert response.result == {"outcome": outcome}


@pytest.mark.parametrize(
    "payload",
    [
        b'{"command":"verify_password"}\n',
        b'{"command":"verify_password","password":""}\n',
        b'{"command":"verify_password","password":123}\n',
        b'{"command":"verify_password","password":"x","path":"/etc"}\n',
        ('{"command":"verify_password","password":"' + "a" * 1025 + '"}\n').encode(),
    ],
)
async def test_verify_password_rejects_invalid_requests(
    server: WorkerServer, payload: bytes
) -> None:
    assert await _error_code(server.path, payload) is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "payload",
    [
        b'{"command":"wake_target"}\n',
        b'{"command":"shutdown_target"}\n',
        b'{"command":"vm_start"}\n',
        b'{"command":"vm_stop"}\n',
        b'{"command":"vm_force_stop"}\n',
        b'{"command":"sync_start"}\n',
        f'{{"command":"sync_cancel","job_id":"{JOB_ID}"}}\n'.encode(),
        f'{{"command":"sync_confirm_deletions","job_id":"{JOB_ID}"}}\n'.encode(),
    ],
)
async def test_other_commands_are_not_implemented_yet(server: WorkerServer, payload: bytes) -> None:
    assert await _error_code(server.path, payload) is ErrorCode.NOT_IMPLEMENTED


@pytest.mark.parametrize(
    "payload",
    [
        b'{"command":"rm_rf"}\n',
        b'{"command":"get_status","path":"/etc"}\n',
        b"not json\n",
        b'{"command":"get_status"}',
    ],
)
async def test_invalid_requests_are_rejected(server: WorkerServer, payload: bytes) -> None:
    assert await _error_code(server.path, payload) is ErrorCode.INVALID_REQUEST


async def test_oversized_request_is_rejected(server: WorkerServer) -> None:
    payload = b"a" * (MAX_REQUEST_BYTES * 2) + b"\n"
    assert await _error_code(server.path, payload) is ErrorCode.INVALID_REQUEST


async def test_silent_client_times_out(socket_dir: Path) -> None:
    worker = await _start(socket_dir / "worker.sock", os.getuid(), read_timeout=0.2)
    try:
        reader, writer = await asyncio.open_unix_connection(str(worker.path))
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        await writer.wait_closed()
        response = decode(line)
        assert isinstance(response, ErrorResponse)
        assert response.message == "request timed out"
    finally:
        await worker.close()


async def test_foreign_uid_is_rejected(socket_dir: Path) -> None:
    worker = await _start(socket_dir / "worker.sock", os.getuid() + 1)
    try:
        code = await _error_code(worker.path, b'{"command":"get_status"}\n')
        assert code is ErrorCode.FORBIDDEN
    finally:
        await worker.close()


async def test_socket_and_directory_permissions(server: WorkerServer) -> None:
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o660
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o750


async def test_socket_is_removed_on_close(socket_dir: Path) -> None:
    worker = await _start(socket_dir / "worker.sock", os.getuid())
    await worker.close()
    assert not worker.path.exists()


def test_stale_socket_is_replaced(socket_dir: Path) -> None:
    socket_dir.mkdir()
    path = socket_dir / "worker.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()
    prepare_socket_path(path)
    assert not path.exists()


def test_regular_file_is_never_removed(socket_dir: Path) -> None:
    socket_dir.mkdir()
    path = socket_dir / "worker.sock"
    path.write_text("wichtig")
    with pytest.raises(SocketPathError, match="not a socket"):
        prepare_socket_path(path)
    assert path.read_text() == "wichtig"
