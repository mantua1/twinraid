"""Web-Prozess und Worker über einen echten Unix-Socket."""

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from tests.support.passwords import TEST_PASSWORD
from tests.support.worker import make_handler
from tests.web.helpers import BASE_URL, CLIENT_IP, make_app
from twinraid.shared.actions import ActionBlocker
from twinraid.shared.ipc import ErrorCode, SyncStart
from twinraid.shared.passwords import VerifyOutcome
from twinraid.web.worker_client import WorkerClient, WorkerCommandError, WorkerUnavailableError
from twinraid.worker.server import WorkerServer

pytestmark = pytest.mark.anyio


@pytest.fixture
async def worker_socket() -> AsyncIterator[Path]:
    base = Path(tempfile.mkdtemp(prefix="tr-"))
    handler = make_handler(base)
    server = WorkerServer(base / "w.sock", os.getuid(), handler)
    await server.start()
    try:
        yield server.path
    finally:
        await server.close()
        shutil.rmtree(base)


async def test_login_and_status_through_real_socket(worker_socket: Path, tmp_path: Path) -> None:
    app = make_app(tmp_path, WorkerClient(worker_socket))
    transport = httpx2.ASGITransport(app=app, client=(CLIENT_IP, 50000))
    async with httpx2.AsyncClient(
        transport=transport, base_url=BASE_URL, headers={"Origin": BASE_URL}
    ) as client:
        assert (await client.get("/api/status")).status_code == 401
        wrong = await client.post("/api/auth/login", json={"password": "falsch-falsch"})
        assert wrong.status_code == 401
        ok = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert ok.status_code == 200
        response = await client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["vm"] == {"state": "unknown", "reason": None}
    assert response.json()["job_running"] is False
    assert response.json()["password_set"] is True


async def test_client_verifies_passwords(worker_socket: Path) -> None:
    client = WorkerClient(worker_socket)
    assert await client.verify_password(TEST_PASSWORD) is VerifyOutcome.VALID
    assert await client.verify_password("falsch-falsch") is VerifyOutcome.INVALID


async def test_client_reports_unavailable_worker(tmp_path: Path) -> None:
    client = WorkerClient(tmp_path / "missing.sock", timeout=1)
    with pytest.raises(WorkerUnavailableError):
        await client.get_status()
    with pytest.raises(WorkerUnavailableError):
        await client.verify_password("irgendwas")


async def test_client_reports_rejected_command(worker_socket: Path) -> None:
    client = WorkerClient(worker_socket)
    with pytest.raises(WorkerCommandError) as excinfo:
        await client.send(SyncStart(command="sync_start"))
    # In v0.5.1 gibt es nur den Probelauf; der Sync kommt mit v0.5.2.
    assert excinfo.value.error is ErrorCode.ACTION_BLOCKED
    assert excinfo.value.blocked_by is ActionBlocker.NEXT_VERSION
