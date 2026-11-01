"""Öffentliche Routen enthalten keine Konfigurationswerte (Konzept 5.3).

Der Container-Starttest prüft zusätzlich die echten, gebauten Frontend-Dateien.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CANARY_CONFIG, EXAMPLE_CONFIG
from tests.web.helpers import build_frontend, login, make_app, make_client
from tests.web.test_route_protection import EXPECTED_PUBLIC
from twinraid.config import load_config
from twinraid.config.sensitive import sensitive_values

# Passend zur Testkonfiguration: Adresse aus web.tls, Client aus allowed_networks.
CANARY_BASE_URL = "https://10.77.31.50:8443"
CANARY_CLIENT_IP = "10.77.31.9"

# Je öffentlicher Route ein Abruf: (Methode, Pfad, JSON-Inhalt).
PUBLIC_REQUESTS: dict[str, tuple[str, str, dict[str, str] | None]] = {
    "/health": ("GET", "/health", None),
    "/": ("GET", "/", None),
    "/favicon.svg": ("GET", "/favicon.svg", None),
    "/assets": ("GET", "/assets/app.js", None),
    "/api/auth/state": ("GET", "/api/auth/state", None),
    "/api/auth/login": ("POST", "/api/auth/login", {"password": "falsches-passwort"}),
}


def _canary_client(tmp_path: Path, frontend_dir: Path | None = None) -> TestClient:
    app = make_app(tmp_path, config_path=CANARY_CONFIG, frontend_dir=frontend_dir)
    return make_client(app, ip=CANARY_CLIENT_IP, base_url=CANARY_BASE_URL)


def _leaks(body: str) -> list[str]:
    lowered = body.lower()
    return [v for v in sensitive_values(load_config(CANARY_CONFIG)) if v.lower() in lowered]


def test_public_requests_cover_every_public_route() -> None:
    assert set(PUBLIC_REQUESTS) == EXPECTED_PUBLIC


def test_canary_values_are_distinctive() -> None:
    """Testwerte dürfen nicht zufällig in Code oder Übersetzungen vorkommen."""
    canary = sensitive_values(load_config(CANARY_CONFIG))
    example = sensitive_values(load_config(EXAMPLE_CONFIG))
    assert canary.isdisjoint(example)
    assert all(len(value) >= 10 for value in canary), sorted(canary)


def test_sensitive_values_cover_all_kinds() -> None:
    values = sensitive_values(load_config(CANARY_CONFIG))
    expected = {
        "10.77.31.0/24",  # Netz
        "10.77.31.41",  # Host Quelle
        "10.77.31.42",  # Host Ziel
        "10.77.31.255",  # Broadcast
        "02:7A:C3:91:5E:4D",  # MAC
        "02-7A-C3-91-5E-4D",
        "027AC3915E4D",
        "canary-source-name-7q",  # Servername
        "canary-vm-unraid-7q",  # VM-Name in Unraid
        "canaryshare7q",  # Share
        "canarycontainer7q",  # Container
        "10.77.31.50",  # IP im Zertifikat
        "canary-host-7q.home.arpa",  # Hostname im Zertifikat
    }
    assert expected <= values
    # Loopback ist kein schützenswerter Wert und taucht deshalb nicht auf.
    assert "127.0.0.1" not in values


@pytest.mark.parametrize("route", sorted(PUBLIC_REQUESTS))
def test_public_route_contains_no_config_values(tmp_path: Path, route: str) -> None:
    method, path, body = PUBLIC_REQUESTS[route]
    response = _canary_client(tmp_path).request(method, path, json=body)
    assert response.status_code in {200, 401}
    assert _leaks(response.text) == []
    assert _leaks(str(response.headers)) == []


def test_config_values_only_via_protected_api(tmp_path: Path) -> None:
    client = _canary_client(tmp_path)
    denied = client.get("/api/info")
    assert denied.status_code == 401
    assert "canary" not in denied.text
    login(client)
    assert client.get("/api/info").json()["names"]["source"] == "canary-source-name-7q"


def test_check_detects_a_leak(tmp_path: Path) -> None:
    """Gegenprobe: Stünde ein Wert in einer ausgelieferten Datei, fände der Test ihn."""
    frontend = build_frontend(tmp_path)
    (frontend / "index.html").write_text("<!doctype html><p>Server 10.77.31.41</p>")
    response = _canary_client(tmp_path, frontend).get("/")
    assert _leaks(response.text) == ["10.77.31.41"]
