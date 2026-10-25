"""Hilfsmittel für Web-Tests."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import EXAMPLE_CONFIG
from tests.support.passwords import TEST_PASSWORD
from tests.support.tls import install_test_runtime
from twinraid.config import load_config
from twinraid.shared.actions import ActionBlocker, ActionName, ActionsSnapshot
from twinraid.shared.addresses import configured, resolve_addresses
from twinraid.shared.mode import RunMode
from twinraid.shared.passwords import VerifyOutcome
from twinraid.shared.source_guard import GuardState, SourceGuardStatus
from twinraid.shared.status import UNKNOWN, StatusSnapshot
from twinraid.shared.sync import SyncLevels, SyncStatus
from twinraid.web.app import WebApp, WebSettings, create_app
from twinraid.web.db import WebDatabase
from twinraid.web.events import EventTimings
from twinraid.web.worker_client import WorkerApi, WorkerCommandError, WorkerUnavailableError

# Kurze Zeiten für Tests. Ein Datenstrom endet spätestens nach einer halben Sekunde,
# damit kein Test an einer offenen Verbindung hängen bleibt.
TEST_EVENT_TIMINGS = EventTimings(
    poll_seconds=0.01, session_check_seconds=0.005, keepalive_seconds=0.05, max_stream_seconds=0.5
)

# Verwendete Adresse ohne Konfiguration: die erkannte Testschnittstelle (tests/conftest.py).
TEST_ADDRESSES = configured([ip_address("192.168.10.20")])

# Passend zu config.example.yaml: erkannte Adresse, Client aus dem Client-Netz.
BASE_URL = "https://192.168.10.20:8443"
CLIENT_IP = "192.168.20.5"


def unknown_status(password_set: bool = True, job_running: bool = False) -> StatusSnapshot:
    return StatusSnapshot(
        source=UNKNOWN,
        target=UNKNOWN,
        vm=UNKNOWN,
        checked_at=None,
        job_running=job_running,
        password_set=password_set,
        actions=ActionsSnapshot.blocked(ActionBlocker.TARGET_UNKNOWN),
        source_guard=SourceGuardStatus(state=GuardState.NOT_MOUNTED),
        sync=SyncStatus(
            levels=SyncLevels(configured=(), source=None, target=None, target_checked_at=None)
        ),
    )


class FakeWorker:
    """Simulierter Worker. Zählt alle Aufrufe, damit Tests prüfen können, was gesendet wurde."""

    def __init__(self, available: bool = True, password: str | None = TEST_PASSWORD) -> None:
        self.available = available
        self.password = password
        self.job_running = False
        self.status: StatusSnapshot | None = None
        # Wird vor jeder Statusabfrage aufgerufen, z. B. um eine Uhr weiterzustellen.
        self.on_status: Callable[[int], None] | None = None
        self.calls: list[str] = []
        # True: Jede Abfrage hat einen neuen Prüfzeitpunkt, wie beim echten Worker.
        self.stamp_checks = False
        self.verify_calls = 0
        self.status_calls = 0
        # Antwort auf Aktionen: None nimmt an, sonst lehnt der Worker mit diesem Fehler ab.
        self.action_error: WorkerCommandError | None = None
        self.actions: list[ActionName] = []
        self.cancelled: list[str] = []

    async def get_status(self) -> StatusSnapshot:
        self.calls.append("get_status")
        self.status_calls += 1
        if self.on_status is not None:
            self.on_status(self.status_calls)
        if not self.available:
            raise WorkerUnavailableError("down")
        if self.status is not None:
            return self.status
        status = unknown_status(self.password is not None, self.job_running)
        if self.stamp_checks:
            checked = datetime.fromtimestamp(self.status_calls, UTC)
            status = status.model_copy(update={"checked_at": checked})
        return status

    async def verify_password(self, password: str) -> VerifyOutcome:
        self.calls.append("verify_password")
        self.verify_calls += 1
        if not self.available:
            raise WorkerUnavailableError("down")
        if self.password is None:
            return VerifyOutcome.NOT_SET
        return VerifyOutcome.VALID if password == self.password else VerifyOutcome.INVALID

    async def request_action(self, action: ActionName) -> None:
        self.calls.append(action.value)
        if not self.available:
            raise WorkerUnavailableError("down")
        if self.action_error is not None:
            raise self.action_error
        self.actions.append(action)

    async def cancel_sync(self, job_id: str) -> None:
        self.calls.append("sync_cancel")
        if not self.available:
            raise WorkerUnavailableError("down")
        if self.action_error is not None:
            raise self.action_error
        self.cancelled.append(job_id)


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
    event_timings: EventTimings = TEST_EVENT_TIMINGS,
    mode: RunMode = RunMode.MOCK,
) -> WebApp:
    config = load_config(config_path)
    # Wie twinraid-init: konfigurierte Adressen oder die erkannte Testschnittstelle.
    addresses = resolve_addresses(config.web.tls)
    database = WebDatabase(tmp_path / "web.sqlite")
    database.initialize()
    runtime_tls = tmp_path / "run-tls"
    if not (runtime_tls / "cert.pem").exists():
        install_test_runtime(runtime_tls, addresses)
    settings = WebSettings(
        mode=mode,
        config=config,
        frontend_dir=frontend_dir if frontend_dir is not None else build_frontend(tmp_path),
        runtime_tls_dir=runtime_tls,
        renewal_marker=tmp_path / "tls-renewal.json",
        addresses=addresses,
    )
    return create_app(
        settings,
        worker or FakeWorker(),
        database,
        clock=clock or time.time,
        event_timings=event_timings,
    )


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
