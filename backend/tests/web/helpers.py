"""Hilfsmittel für Web-Tests."""

from pathlib import Path

from fastapi import FastAPI
from starlette.requests import HTTPConnection

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import load_config
from twinraid.shared.mode import RunMode
from twinraid.shared.status import ResourceState, StatusSnapshot
from twinraid.web.app import WebSettings, create_app
from twinraid.web.worker_client import WorkerApi, WorkerUnavailableError

UNKNOWN_STATUS = StatusSnapshot(
    source=ResourceState.UNKNOWN,
    target=ResourceState.UNKNOWN,
    vm=ResourceState.UNKNOWN,
    checked_at=None,
    password_set=True,
)


class AllowAll:
    def is_authenticated(self, connection: HTTPConnection) -> bool:
        return True


class DenyAll:
    def is_authenticated(self, connection: HTTPConnection) -> bool:
        return False


class FakeWorker:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    async def get_status(self) -> StatusSnapshot:
        if not self.available:
            raise WorkerUnavailableError("down")
        return UNKNOWN_STATUS


def build_frontend(base: Path) -> Path:
    frontend = base / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>TwinRaid</title>")
    (frontend / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (frontend / "assets" / "app.js").write_text("console.log('ok')")
    return frontend


def make_app(
    frontend_dir: Path,
    verifier: AllowAll | DenyAll,
    worker: WorkerApi | None = None,
    config_path: Path = EXAMPLE_CONFIG,
) -> FastAPI:
    settings = WebSettings(
        mode=RunMode.MOCK, config=load_config(config_path), frontend_dir=frontend_dir
    )
    return create_app(settings, worker or FakeWorker(), verifier)
