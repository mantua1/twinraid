"""Hilfsmittel für Web-Tests."""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import EXAMPLE_CONFIG
from tests.support.passwords import TEST_PASSWORD
from twinraid.config import load_config
from twinraid.shared.mode import RunMode
from twinraid.shared.passwords import VerifyOutcome
from twinraid.shared.status import ResourceState, StatusSnapshot
from twinraid.shared.tls import generate_self_signed, install_runtime
from twinraid.web.app import WebApp, WebSettings, create_app
from twinraid.web.db import WebDatabase
from twinraid.web.worker_client import WorkerApi, WorkerUnavailableError

# Passend zu config.example.yaml: Adresse aus web.tls, Client aus dem Client-Netz.
BASE_URL = "https://192.168.10.20:8443"
CLIENT_IP = "192.168.20.5"


def unknown_status(password_set: bool = True) -> StatusSnapshot:
    return StatusSnapshot(
        source=ResourceState.UNKNOWN,
        target=ResourceState.UNKNOWN,
        vm=ResourceState.UNKNOWN,
        checked_at=None,
        password_set=password_set,
    )


class FakeWorker:
    def __init__(self, available: bool = True, password: str | None = TEST_PASSWORD) -> None:
        self.available = available
        self.password = password
        self.verify_calls = 0

    async def get_status(self) -> StatusSnapshot:
        if not self.available:
            raise WorkerUnavailableError("down")
        return unknown_status(self.password is not None)

    async def verify_password(self, password: str) -> VerifyOutcome:
        self.verify_calls += 1
        if not self.available:
            raise WorkerUnavailableError("down")
        if self.password is None:
            return VerifyOutcome.NOT_SET
        return VerifyOutcome.VALID if password == self.password else VerifyOutcome.INVALID


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_frontend(base: Path) -> Path:
    frontend = base / "frontend"
    (frontend / "assets").mkdir(parents=True, exist_ok=True)
    (frontend / "index.html").write_text("<!doctype html><title>TwinRaid</title>")
    (frontend / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (frontend / "assets" / "app.js").write_text("console.log('ok')")
    return frontend


def make_app(
    tmp_path: Path,
    worker: WorkerApi | None = None,
    config_path: Path = EXAMPLE_CONFIG,
    frontend_dir: Path | None = None,
    clock: Callable[[], float] | None = None,
) -> WebApp:
    config = load_config(config_path)
    database = WebDatabase(tmp_path / "web.sqlite")
    database.initialize()
    runtime_tls = tmp_path / "run-tls"
    if not (runtime_tls / "cert.pem").exists():
        cert, key = generate_self_signed(
            config.web.tls.ip_addresses, config.web.tls.hostnames, datetime.now(UTC)
        )
        install_runtime(runtime_tls, cert, key, "self-signed", os.getgid())
    settings = WebSettings(
        mode=RunMode.MOCK,
        config=config,
        frontend_dir=frontend_dir if frontend_dir is not None else build_frontend(tmp_path),
        runtime_tls_dir=runtime_tls,
    )
    kwargs = {"clock": clock} if clock is not None else {}
    return create_app(settings, worker or FakeWorker(), database, **kwargs)


def make_client(
    app: WebApp, ip: str = CLIENT_IP, base_url: str = BASE_URL, origin: bool = True
) -> TestClient:
    """Client wie ein Browser auf derselben Seite: HTTPS-Adresse und passender Origin."""
    headers = {"Origin": base_url} if origin else {}
    return TestClient(app, base_url=base_url, client=(ip, 50000), headers=headers)


def login(client: TestClient, password: str = TEST_PASSWORD) -> str:
    """Meldet an und gibt das CSRF-Token zurück."""
    response = client.post("/api/auth/login", json={"password": password})
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    assert isinstance(token, str)
    return token
