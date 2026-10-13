"""Geschützt per Default: Nur ausdrücklich markierte Routen sind öffentlich (Konzept 5.3)."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from tests.web.helpers import DenyAll, build_frontend, make_app
from twinraid.shared.mode import RunMode
from twinraid.web.auth import PlaceholderSessionVerifier, is_public_route

# Die vollständige Liste der öffentlichen Routen. Jede Erweiterung muss hier
# bewusst eingetragen werden (später: Login).
EXPECTED_PUBLIC = {"/health", "/", "/favicon.svg", "/assets"}


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return make_app(build_frontend(tmp_path), DenyAll())


def _route_path(route: object) -> str:
    assert isinstance(route, APIRoute | Mount)
    return route.path


def test_route_table_is_flat(app: FastAPI) -> None:
    """Nur einfache Routen und Mounts: Die Middleware muss jede Route erkennen können.

    Eingebundene Router (include_router) erscheinen in FastAPI als eigene
    Objekte. Die Middleware würde sie zwar sicher als geschützt behandeln,
    @public würde dort aber nicht greifen.
    """
    for route in app.routes:
        assert type(route) in {APIRoute, Mount}, f"unexpected route type: {route!r}"


def test_only_expected_routes_are_public(app: FastAPI) -> None:
    public = {_route_path(route) for route in app.routes if is_public_route(route)}
    assert public == EXPECTED_PUBLIC


def test_every_other_route_rejects_requests_without_session(app: FastAPI) -> None:
    client = TestClient(app)
    checked = 0
    for route in app.routes:
        if is_public_route(route):
            continue
        if isinstance(route, APIRoute):
            for method in sorted(route.methods or ()):
                response = client.request(method, route.path.replace("{", "x").replace("}", ""))
                assert response.status_code == 401, f"{method} {route.path}"
                checked += 1
        elif isinstance(route, Mount):
            response = client.get(f"{route.path}/x")
            assert response.status_code == 401, route.path
            checked += 1
        else:
            pytest.fail(f"unexpected route type: {route!r}")
    assert checked > 0


def test_public_routes_work_without_session(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.get("/health").text == "ok"
    assert client.get("/").status_code == 200
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/assets/app.js").status_code == 200


def test_new_unmarked_route_is_protected_by_default(app: FastAPI) -> None:
    @app.get("/api/forgotten")
    async def forgotten() -> dict[str, str]:
        return {"secret": "data"}

    response = TestClient(app).get("/api/forgotten")
    assert response.status_code == 401
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "path", ["/does-not-exist", "/api", "/api/", "/docs", "/openapi.json", "/redoc"]
)
def test_unknown_paths_require_session(app: FastAPI, path: str) -> None:
    assert TestClient(app).get(path).status_code == 401


def test_method_mismatch_on_public_route_does_not_leak(app: FastAPI) -> None:
    assert TestClient(app).post("/health").status_code == 405


def test_websocket_is_rejected_without_session(app: FastAPI) -> None:
    with pytest.raises(WebSocketDisconnect), TestClient(app).websocket_connect("/api/ws"):
        pass


def test_placeholder_allows_only_mock_mode() -> None:
    # Die Verbindung wird vom Platzhalter nicht ausgewertet.
    connection = None
    assert PlaceholderSessionVerifier(RunMode.MOCK).is_authenticated(connection) is True  # type: ignore[arg-type]
    assert PlaceholderSessionVerifier(RunMode.REAL).is_authenticated(connection) is False  # type: ignore[arg-type]
