"""Geschützt per Default: Nur ausdrücklich markierte Routen sind öffentlich (Konzept 5.3)."""

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from tests.web.helpers import login, make_app, make_client
from twinraid.web.app import WebApp
from twinraid.web.auth import is_public_route

# Die vollständige Liste der öffentlichen Routen. Jede Erweiterung muss hier
# bewusst eingetragen werden.
EXPECTED_PUBLIC = {
    "/health",
    "/",
    "/favicon.svg",
    "/assets",
    "/api/auth/state",
    "/api/auth/login",
}


@pytest.fixture
def app(tmp_path: Path) -> WebApp:
    return make_app(tmp_path)


def _route_path(route: object) -> str:
    assert isinstance(route, APIRoute | Mount)
    return route.path


def test_route_table_is_flat(app: WebApp) -> None:
    """Nur einfache Routen und Mounts: Die Middleware muss jede Route erkennen können.

    Eingebundene Router (include_router) erscheinen in FastAPI als eigene
    Objekte. Die Middleware würde sie zwar sicher als geschützt behandeln,
    @public würde dort aber nicht greifen.
    """
    for route in app.api.routes:
        assert type(route) in {APIRoute, Mount}, f"unexpected route type: {route!r}"


def test_only_expected_routes_are_public(app: WebApp) -> None:
    public = {_route_path(route) for route in app.api.routes if is_public_route(route)}
    assert public == EXPECTED_PUBLIC


def test_every_other_route_rejects_requests_without_session(app: WebApp) -> None:
    client = make_client(app)
    checked = 0
    for route in app.api.routes:
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
    assert checked >= 7


# Gültige Werte für Pfadparameter, damit jede Route einmal erfolgreich aufgerufen wird.
PATH_SAMPLES = {"{action}": "vm_start"}


def test_every_api_route_works_with_session(app: WebApp) -> None:
    client = make_client(app)
    csrf = login(client)
    for route in app.api.routes:
        if is_public_route(route) or not isinstance(route, APIRoute):
            continue
        path = route.path
        for placeholder, sample in PATH_SAMPLES.items():
            path = path.replace(placeholder, sample)
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue
            response = client.request(method, path, headers={"X-CSRF-Token": csrf})
            # 202: Der Worker hat eine Aktion angenommen.
            assert response.status_code in (200, 202), f"{method} {path}: {response.text}"
            if route.path == "/api/auth/logout":
                csrf = login(client)


def test_public_routes_work_without_session(app: WebApp) -> None:
    client = make_client(app)
    assert client.get("/health").text == "ok"
    assert client.get("/").status_code == 200
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/auth/state").status_code == 200
    assert client.post("/api/auth/login", json={"password": "x"}).status_code == 401


def test_new_unmarked_route_is_protected_by_default(app: WebApp) -> None:
    @app.api.get("/api/forgotten")
    async def forgotten() -> dict[str, str]:
        return {"secret": "data"}

    response = make_client(app).get("/api/forgotten")
    assert response.status_code == 401
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "path", ["/does-not-exist", "/api", "/api/", "/docs", "/openapi.json", "/redoc"]
)
def test_unknown_paths_require_session(app: WebApp, path: str) -> None:
    assert make_client(app).get(path).status_code == 401


def test_method_mismatch_on_public_route_does_not_leak(app: WebApp) -> None:
    assert make_client(app).post("/health").status_code == 405


def test_websocket_is_rejected_without_session(app: WebApp) -> None:
    with pytest.raises(WebSocketDisconnect), make_client(app).websocket_connect("/api/ws"):
        pass
