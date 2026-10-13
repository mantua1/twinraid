"""Zugriffsschutz: Jede Route ist geschützt, außer sie ist ausdrücklich öffentlich.

Die Prüfung läuft als ASGI-Middleware vor dem Routing. Dadurch gilt sie auch
für Mounts, unbekannte Pfade und Routen, die später hinzukommen. Öffentliche
Routen werden mit @public markiert (Konzept 5.3).
"""

from typing import Protocol

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, Mount, Route, Router
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from twinraid.shared.mode import RunMode

_PUBLIC_ATTRIBUTE = "__twinraid_public__"


def public[T](target: T) -> T:
    """Markiert eine Endpunkt-Funktion oder eine gemountete ASGI-App als öffentlich."""
    setattr(target, _PUBLIC_ATTRIBUTE, True)
    return target


def is_public_route(route: BaseRoute) -> bool:
    if isinstance(route, Route):
        target: object = route.endpoint
    elif isinstance(route, Mount):
        target = route.app
    else:
        return False
    return getattr(target, _PUBLIC_ATTRIBUTE, False) is True


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


class SessionVerifier(Protocol):
    def is_authenticated(self, connection: HTTPConnection) -> bool: ...


class PlaceholderSessionVerifier:
    """Platzhalter bis zur echten Login-Prüfung in Phase 2.

    Lässt Anfragen nur im Mock-Modus durch (Konzept 5.3). In jedem anderen
    Modus ist ohne echte Login-Prüfung nichts erreichbar.
    """

    def __init__(self, mode: RunMode) -> None:
        self._allow = mode is RunMode.MOCK

    def is_authenticated(self, connection: HTTPConnection) -> bool:
        return self._allow


class AuthMiddleware:
    def __init__(self, app: ASGIApp, router: Router, verifier: SessionVerifier) -> None:
        self._app = app
        self._router = router
        self._verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        route = find_route(self._router, scope)
        if route is not None and is_public_route(route):
            await self._app(scope, receive, send)
            return
        if self._verifier.is_authenticated(HTTPConnection(scope)):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        response = JSONResponse({"detail": "not_authenticated"}, status_code=401)
        await response(scope, receive, send)
