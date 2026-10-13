"""ASGI-Anwendung des Web-Prozesses (Konzept 5.1 und 5.3)."""

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from twinraid import __version__
from twinraid.config import TwinRaidConfig
from twinraid.shared.mode import RunMode
from twinraid.shared.passwords import VerifyOutcome
from twinraid.shared.status import StatusSnapshot
from twinraid.shared.tls import TlsError, certificate_info, read_runtime
from twinraid.web.audit import AuditAction, AuditLog, AuditResult
from twinraid.web.auth import AuthMiddleware, public, session_of
from twinraid.web.db import WebDatabase
from twinraid.web.ratelimit import LoginLimiter, ip_key, normalize_ip
from twinraid.web.schemas import (
    AppInfo,
    AuditEntryOut,
    AuditList,
    AuthState,
    DisplayNames,
    LoginRequest,
    SessionInfo,
    TlsInfo,
)
from twinraid.web.security import (
    BodyLimitMiddleware,
    HostCheckMiddleware,
    NetworkFilterMiddleware,
    OriginCheckMiddleware,
    SecurityHeadersMiddleware,
)
from twinraid.web.sessions import (
    MAX_LIFETIME_SECONDS,
    SESSION_COOKIE,
    Session,
    SessionStore,
)
from twinraid.web.worker_client import WorkerApi, WorkerCommandError, WorkerUnavailableError

logger = logging.getLogger(__name__)

MAX_CONCURRENT_LOGIN_CHECKS = 2
BUSY_RETRY_AFTER_SECONDS = 5
PURGE_INTERVAL_SECONDS = 60 * 60


async def _run[**P, R](func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Blockierende Datenbankzugriffe in einem Thread ausführen."""
    return await asyncio.to_thread(func, *args, **kwargs)


def client_ip(connection: HTTPConnection | Scope) -> str | None:
    scope = connection.scope if isinstance(connection, HTTPConnection) else connection
    client = scope.get("client")
    if not client:
        return None
    try:
        return str(normalize_ip(str(client[0])))
    except ValueError:
        return None


@dataclass(frozen=True)
class WebSettings:
    mode: RunMode
    config: TwinRaidConfig
    frontend_dir: Path
    runtime_tls_dir: Path


class SessionAuthenticator:
    def __init__(self, sessions: SessionStore, audit: AuditLog) -> None:
        self._sessions = sessions
        self._audit = audit

    async def authenticate(self, connection: HTTPConnection) -> Session | None:
        token = connection.cookies.get(SESSION_COOKIE)
        if token is None:
            return None
        lookup = await _run(self._sessions.lookup, token)
        if lookup.expired:
            await _run(
                self._audit.record, AuditAction.SESSION, AuditResult.EXPIRED, client_ip(connection)
            )
        return lookup.session


class WebApp:
    """Äußere Hülle: Security-Header und Netzfilter liegen außerhalb von FastAPI.

    So bekommen auch Fehlerantworten von FastAPI selbst (500) die Header, und
    der Netzfilter läuft vor allem anderen.
    """

    def __init__(self, api: FastAPI, outer: ASGIApp) -> None:
        self.api = api
        self._outer = outer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._outer(scope, receive, send)


def create_app(
    settings: WebSettings,
    worker: WorkerApi,
    database: WebDatabase,
    clock: Callable[[], float] = time.time,
) -> WebApp:
    config = settings.config
    sessions = SessionStore(database, config.web.session_idle_minutes * 60, clock)
    limiter = LoginLimiter(database, clock)
    audit = AuditLog(database, clock)
    authenticator = SessionAuthenticator(sessions, audit)

    async def purge_all() -> None:
        for purge in (sessions.purge_expired, limiter.purge, audit.purge):
            await _run(purge)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async def purge_forever() -> None:
            while True:
                await asyncio.sleep(PURGE_INTERVAL_SECONDS)
                await purge_all()

        # Beim Start sofort aufräumen, danach stündlich.
        await purge_all()
        task = asyncio.create_task(purge_forever())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # Keine automatische API-Dokumentation: weniger Angriffsfläche.
    api = FastAPI(
        title="TwinRaid", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @api.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # Ohne Details: FastAPI würde sonst die Eingabe (z. B. ein Passwort) zurückgeben.
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    # Alle Routen direkt an der App, ohne include_router() (siehe web/auth.py).
    _register_public_routes(api, settings.frontend_dir)
    _register_auth_routes(api, settings, worker, sessions, limiter, audit, authenticator, clock)
    _register_api_routes(api, settings, worker, audit)

    async def on_csrf_reject(scope: Scope) -> None:
        await _run(
            audit.record, AuditAction.CSRF_CHECK, AuditResult.REJECTED, client_ip(scope), True
        )

    def on_origin_reject(scope: Scope) -> None:
        audit.record(AuditAction.ORIGIN_CHECK, AuditResult.REJECTED, client_ip(scope), True)

    # Innerhalb von FastAPI, von innen nach außen hinzugefügt.
    api.add_middleware(
        AuthMiddleware,
        router=api.router,
        authenticator=authenticator,
        on_csrf_reject=on_csrf_reject,
    )
    api.add_middleware(OriginCheckMiddleware, on_reject=on_origin_reject)
    api.add_middleware(BodyLimitMiddleware)
    api.add_middleware(HostCheckMiddleware, allowed_hosts=config.web.tls.allowed_hosts())
    outer = SecurityHeadersMiddleware(NetworkFilterMiddleware(api, config.web.allowed_networks))
    return WebApp(api, outer)


class _OptionalStaticFiles(StaticFiles):
    """StaticFiles, das bei fehlendem Verzeichnis 404 liefert statt eines Fehlers."""

    async def check_config(self) -> None:
        # StaticFiles meldet ein fehlendes Verzeichnis als RuntimeError. Danach
        # findet es keine Dateien und antwortet mit 404.
        with contextlib.suppress(RuntimeError):
            await super().check_config()


def _register_public_routes(api: FastAPI, frontend_dir: Path) -> None:
    @api.get("/health", response_class=PlainTextResponse)
    @public
    async def health() -> str:
        return "ok"

    # Die Oberfläche ist öffentlich, weil sie die Login-Seite enthält.
    # Sie enthält keine Daten (Konzept 5.3).
    @api.get("/", include_in_schema=False)
    @public
    def index() -> Response:
        return _frontend_file(frontend_dir / "index.html", "text/html")

    @api.get("/favicon.svg", include_in_schema=False)
    @public
    def favicon() -> Response:
        return _frontend_file(frontend_dir / "favicon.svg", "image/svg+xml")

    assets = _OptionalStaticFiles(directory=frontend_dir / "assets", check_dir=False)
    api.mount("/assets", public(assets), name="assets")


def _session_info(session: Session, sessions: SessionStore) -> SessionInfo:
    return SessionInfo(
        csrf_token=session.csrf_token,
        expires_at=datetime.fromtimestamp(session.expires_at(sessions.idle_seconds), UTC),
        idle_timeout_seconds=int(sessions.idle_seconds),
    )


def _too_many(detail: str, retry_after: int) -> JSONResponse:
    return JSONResponse(
        {"detail": detail, "retry_after": retry_after},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


def _register_auth_routes(
    api: FastAPI,
    settings: WebSettings,
    worker: WorkerApi,
    sessions: SessionStore,
    limiter: LoginLimiter,
    audit: AuditLog,
    authenticator: SessionAuthenticator,
    clock: Callable[[], float],
) -> None:
    login_slots = asyncio.Semaphore(MAX_CONCURRENT_LOGIN_CHECKS)

    @api.get("/api/auth/state")
    @public
    async def auth_state(request: Request) -> AuthState:
        session = await authenticator.authenticate(request)
        try:
            password_set: bool | None = (await worker.get_status()).password_set
        except WorkerUnavailableError, WorkerCommandError:
            password_set = None
        return AuthState(
            authenticated=session is not None, mode=settings.mode, password_set=password_set
        )

    @api.post("/api/auth/login")
    @public
    async def login(request: Request, body: LoginRequest) -> Response:
        ip = client_ip(request)
        key = ip_key(ip) if ip else "unknown"
        locked_until = await _run(limiter.locked_until, key)
        if locked_until is not None:
            await _run(audit.record, AuditAction.LOGIN, AuditResult.LOCKED, ip, True)
            return _too_many("locked", max(1, math.ceil(locked_until - clock())))
        if login_slots.locked():
            await _run(audit.record, AuditAction.LOGIN, AuditResult.BUSY, ip, True)
            return _too_many("busy", BUSY_RETRY_AFTER_SECONDS)
        async with login_slots:
            delay = await _run(limiter.global_delay)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                outcome = await worker.verify_password(body.password)
            except WorkerUnavailableError, WorkerCommandError:
                return JSONResponse({"detail": "worker_unavailable"}, status_code=503)

        if outcome is VerifyOutcome.NOT_SET:
            await _run(audit.record, AuditAction.LOGIN, AuditResult.PASSWORD_NOT_SET, ip)
            return JSONResponse({"detail": "password_not_set"}, status_code=401)
        if outcome is VerifyOutcome.INVALID:
            lock_started = await _run(limiter.record_failure, key)
            await _run(audit.record, AuditAction.LOGIN, AuditResult.INVALID_PASSWORD, ip)
            if lock_started:
                await _run(audit.record, AuditAction.LOGIN, AuditResult.LOCKOUT_STARTED, ip)
                logger.warning("Login locked for %s after repeated failures", key)
            return JSONResponse({"detail": "invalid_password"}, status_code=401)

        await _run(limiter.record_success, key)
        previous = await authenticator.authenticate(request)
        if previous is not None:
            # Immer eine neue Session-ID nach dem Login (Schutz vor Session Fixation).
            await _run(sessions.delete, previous.id_hash)
        token, session = await _run(sessions.create)
        await _run(audit.record, AuditAction.LOGIN, AuditResult.SUCCESS, ip)
        response = JSONResponse(_session_info(session, sessions).model_dump(mode="json"))
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=MAX_LIFETIME_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    @api.post("/api/auth/logout")
    async def logout(request: Request) -> Response:
        session = session_of(request)
        if session is not None:
            await _run(sessions.delete, session.id_hash)
        await _run(audit.record, AuditAction.LOGOUT, AuditResult.SUCCESS, client_ip(request))
        response = JSONResponse({"detail": "logged_out"})
        response.delete_cookie(
            SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
        )
        return response

    @api.get("/api/auth/session")
    async def session_details(request: Request) -> SessionInfo:
        session = session_of(request)
        if session is None:  # pragma: no cover - die Middleware lässt das nicht zu
            raise HTTPException(status_code=401, detail="not_authenticated")
        return _session_info(session, sessions)


def _register_api_routes(
    api: FastAPI, settings: WebSettings, worker: WorkerApi, audit: AuditLog
) -> None:
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

    @api.get("/api/info")
    async def app_info() -> AppInfo:
        return info

    @api.get("/api/status")
    async def status() -> StatusSnapshot:
        try:
            return await worker.get_status()
        except WorkerUnavailableError:
            raise HTTPException(status_code=503, detail="worker_unavailable") from None
        except WorkerCommandError:
            raise HTTPException(status_code=502, detail="worker_error") from None

    @api.get("/api/audit")
    async def audit_entries() -> AuditList:
        entries = await _run(audit.recent, 50)
        return AuditList(
            entries=[
                AuditEntryOut(at=e.at, client_ip=e.client_ip, action=e.action, result=e.result)
                for e in entries
            ]
        )

    def _active_certificate() -> tuple[bytes, TlsInfo]:
        try:
            certificate, source = read_runtime(settings.runtime_tls_dir)
            pem = (settings.runtime_tls_dir / "cert.pem").read_bytes()
        except TlsError, OSError:
            raise HTTPException(status_code=503, detail="certificate_unavailable") from None
        details = certificate_info(certificate)
        now = datetime.now(UTC)
        return pem, TlsInfo(
            source=source,
            fingerprint_sha256=details.fingerprint,
            not_after=details.not_after,
            days_left=details.days_left(now),
            expires_soon=details.expires_soon(now),
        )

    @api.get("/api/tls")
    def tls_info() -> TlsInfo:
        return _active_certificate()[1]

    @api.get("/api/tls/certificate")
    def tls_certificate() -> Response:
        pem, _ = _active_certificate()
        return Response(
            pem,
            media_type="application/x-pem-file",
            headers={"Content-Disposition": 'attachment; filename="twinraid.crt"'},
        )


def _frontend_file(path: Path, media_type: str) -> Response:
    if not path.is_file():
        return PlainTextResponse("frontend not built", status_code=404)
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})
