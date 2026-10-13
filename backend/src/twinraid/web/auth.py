"""Zugriffsschutz: Jede Route ist geschützt, außer sie ist ausdrücklich öffentlich.

Die Prüfung läuft als ASGI-Middleware vor dem Routing. Dadurch gilt sie auch
für Mounts, unbekannte Pfade und Routen, die später hinzukommen. Öffentliche
Routen werden mit @public markiert (Konzept 5.3). Für zustandsändernde
Requests mit Session prüft die Middleware zusätzlich das CSRF-Token.
"""

import hmac
from collections.abc import Awaitable, Callable
from typing import Protocol

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, Mount, Route, Router
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from twinraid.web.security import UNSAFE_METHODS
from twinraid.web.sessions import Session

_PUBLIC_ATTRIBUTE = "__twinraid_public__"
_PASSIVE_ATTRIBUTE = "__twinraid_passive__"
CSRF_HEADER = "x-csrf-token"
SESSION_STATE_KEY = "twinraid_session"


def public[T](target: T) -> T:
    """Markiert eine Endpunkt-Funktion oder eine gemountete ASGI-App als öffentlich."""
    setattr(target, _PUBLIC_ATTRIBUTE, True)
    return target


def passive[T](target: T) -> T:
    """Markiert eine geschützte Route, deren Aufruf die Session nicht verlängert.

    Für den Live-Datenstrom: Ein offenes Dashboard zählt nicht als Aktivität,
    auch nicht beim automatischen Neuverbinden (Konzept 5.3).
    """
    setattr(target, _PASSIVE_ATTRIBUTE, True)
    return target


def _route_target(route: BaseRoute) -> object:
    if isinstance(route, Route):
        return route.endpoint
    if isinstance(route, Mount):
        return route.app
    return None


def is_public_route(route: BaseRoute) -> bool:
    return getattr(_route_target(route), _PUBLIC_ATTRIBUTE, False) is True


def is_passive_route(route: BaseRoute) -> bool:
    return getattr(_route_target(route), _PASSIVE_ATTRIBUTE, False) is True


def find_route(router: Router, scope: Scope) -> BaseRoute | None:
    """Ermittelt die Route so, wie Starlette sie auswählen würde."""
    partial: BaseRoute | None = None
    for route in router.routes:
        match, _child_scope = route.matches(scope)
        if match is Match.FULL:
            return route
        if match is Match.PARTIAL and partial is None:
            partial = route
    return partial


class Authenticator(Protocol):
    async def authenticate(
        self, connection: HTTPConnection, touch: bool = True
    ) -> Session | None: ...


def session_of(connection: HTTPConnection) -> Session | None:
    """Session, die die Middleware für diesen Request ermittelt hat."""
    value = connection.scope.get("state", {}).get(SESSION_STATE_KEY)
    return value if isinstance(value, Session) else None


class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        router: Router,
        authenticator: Authenticator,
        on_csrf_reject: Callable[[Scope], Awaitable[None]] | None = None,
    ) -> None:
        self._app = app
        self._router = router
        self._authenticator = authenticator
        self._on_csrf_reject = on_csrf_reject

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        route = find_route(self._router, scope)
        if route is not None and is_public_route(route):
            await self._app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        touch = route is None or not is_passive_route(route)
        session = await self._authenticator.authenticate(connection, touch=touch)
        if session is None:
            await self._reject(scope, receive, send)
            return
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            token = connection.headers.get(CSRF_HEADER, "")
            if not hmac.compare_digest(token.encode(), session.csrf_token.encode()):
                if self._on_csrf_reject is not None:
                    await self._on_csrf_reject(scope)
                response = JSONResponse({"detail": "csrf_rejected"}, status_code=403)
                await response(scope, receive, send)
                return
        scope.setdefault("state", {})[SESSION_STATE_KEY] = session
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        response = JSONResponse({"detail": "not_authenticated"}, status_code=401)
        await response(scope, receive, send)
