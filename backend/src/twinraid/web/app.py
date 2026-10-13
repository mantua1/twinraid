"""FastAPI-Anwendung des Web-Prozesses."""

import contextlib
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from twinraid import __version__
from twinraid.config import TwinRaidConfig
from twinraid.shared.mode import RunMode
from twinraid.shared.status import StatusSnapshot
from twinraid.web.auth import AuthMiddleware, SessionVerifier, public
from twinraid.web.schemas import AppInfo, DisplayNames
from twinraid.web.worker_client import WorkerApi, WorkerCommandError, WorkerUnavailableError


@dataclass(frozen=True)
class WebSettings:
    mode: RunMode
    config: TwinRaidConfig
    frontend_dir: Path


def create_app(
    settings: WebSettings, worker: WorkerApi, session_verifier: SessionVerifier
) -> FastAPI:
    # Keine automatische API-Dokumentation: weniger Angriffsfläche.
    app = FastAPI(title="TwinRaid", docs_url=None, redoc_url=None, openapi_url=None)
    # Alle Routen werden direkt an der App registriert, ohne include_router().
    # So bleibt die Routentabelle flach, und die Middleware sieht genau die
    # Routen, die FastAPI auswählt (siehe web/auth.py und Test zur Routentabelle).
    _register_public_routes(app, settings.frontend_dir)
    _register_api_routes(app, settings, worker)
    app.add_middleware(AuthMiddleware, router=app.router, verifier=session_verifier)
    return app


class _OptionalStaticFiles(StaticFiles):
    """StaticFiles, das bei fehlendem Verzeichnis 404 liefert statt eines Fehlers."""

    async def check_config(self) -> None:
        # StaticFiles meldet ein fehlendes Verzeichnis als RuntimeError. Danach
        # findet es keine Dateien und antwortet mit 404.
        with contextlib.suppress(RuntimeError):
            await super().check_config()


def _register_public_routes(app: FastAPI, frontend_dir: Path) -> None:
    @app.get("/health", response_class=PlainTextResponse)
    @public
    async def health() -> str:
        return "ok"

    # Die Oberfläche ist öffentlich, weil sie die (spätere) Login-Seite enthält.
    # Sie enthält keine Daten (Konzept 5.3).
    @app.get("/", include_in_schema=False)
    @public
    def index() -> Response:
        return _frontend_file(frontend_dir / "index.html", "text/html")

    @app.get("/favicon.svg", include_in_schema=False)
    @public
    def favicon() -> Response:
        return _frontend_file(frontend_dir / "favicon.svg", "image/svg+xml")

    assets = _OptionalStaticFiles(directory=frontend_dir / "assets", check_dir=False)
    app.mount("/assets", public(assets), name="assets")


def _register_api_routes(app: FastAPI, settings: WebSettings, worker: WorkerApi) -> None:
    config = settings.config
    info = AppInfo(
        version=__version__,
        mode=settings.mode,
        default_language=config.web.default_language,
        names=DisplayNames(
            source=config.servers.source.name,
            target=config.servers.target.name,
            vm=config.gaming_vm.display_name,
        ),
    )

    @app.get("/api/info")
    async def app_info() -> AppInfo:
        return info

    @app.get("/api/status")
    async def status() -> StatusSnapshot:
        try:
            return await worker.get_status()
        except WorkerUnavailableError:
            raise HTTPException(status_code=503, detail="worker_unavailable") from None
        except WorkerCommandError:
            raise HTTPException(status_code=502, detail="worker_error") from None


def _frontend_file(path: Path, media_type: str) -> Response:
    if not path.is_file():
        return PlainTextResponse("frontend not built", status_code=404)
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})
