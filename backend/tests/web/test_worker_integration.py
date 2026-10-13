"""Web-Prozess und Worker über einen echten Unix-Socket."""

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tests.web.helpers import AllowAll, build_frontend, make_app
from twinraid.integrations.mocks.status import MockStatusProvider
from twinraid.shared.ipc import ErrorCode, SyncStart
from twinraid.web.worker_client import WorkerClient, WorkerCommandError, WorkerUnavailableError
from twinraid.worker.handler import CommandHandler
from twinraid.worker.server import WorkerServer

pytestmark = pytest.mark.anyio


@pytest.fixture
async def worker_socket() -> AsyncIterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="tr-"))
    server = WorkerServer(base / "w.sock", os.getuid(), CommandHandler(MockStatusProvider()))
    await server.start()
    try:
        yield server.path
    finally:
        await server.close()
        shutil.rmtree(base)


async def test_status_through_real_socket(worker_socket: Path, tmp_path: Path) -> None:
    app = make_app(build_frontend(tmp_path), AllowAll(), WorkerClient(worker_socket))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["vm"] == "unknown"


async def test_client_reports_unavailable_worker(tmp_path: Path) -> None:
    client = WorkerClient(tmp_path / "missing.sock", timeout=1)
    with pytest.raises(WorkerUnavailableError):
        await client.get_status()


async def test_client_reports_rejected_command(worker_socket: Path) -> None:
    client = WorkerClient(worker_socket)
    with pytest.raises(WorkerCommandError) as excinfo:
        await client.send(SyncStart(command="sync_start"))
    assert excinfo.value.error is ErrorCode.NOT_IMPLEMENTED
