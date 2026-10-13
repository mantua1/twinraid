"""Öffentliche Routen enthalten keine Konfigurationswerte (Konzept 5.3).

Der Container-Starttest prüft zusätzlich die echten, gebauten Frontend-Dateien.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CANARY_CONFIG, EXAMPLE_CONFIG
from tests.web.helpers import AllowAll, DenyAll, build_frontend, make_app
from tests.web.test_route_protection import EXPECTED_PUBLIC
from twinraid.config import load_config
from twinraid.config.sensitive import sensitive_values

# Je öffentlicher Route ein abrufbarer Pfad (für den Mount eine Beispieldatei).
PUBLIC_PATHS = {"/health": "/health", "/": "/", "/favicon.svg": "/favicon.svg"}
PUBLIC_PATHS["/assets"] = "/assets/app.js"


def test_public_paths_cover_every_public_route() -> None:
    assert set(PUBLIC_PATHS) == EXPECTED_PUBLIC


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
        "canaryapp7q/canaryexclude7q",  # Pfad
        "canarycontainer7q",  # Container
    }
    assert expected <= values


@pytest.mark.parametrize("path", sorted(PUBLIC_PATHS.values()))
def test_public_route_contains_no_config_values(tmp_path: Path, path: str) -> None:
    app = make_app(build_frontend(tmp_path), DenyAll(), config_path=CANARY_CONFIG)
    response = TestClient(app).get(path)
    assert response.status_code == 200
    body = response.text.lower()
    leaked = [v for v in sensitive_values(load_config(CANARY_CONFIG)) if v.lower() in body]
    assert leaked == []


def test_config_values_only_via_protected_api(tmp_path: Path) -> None:
    frontend = build_frontend(tmp_path)
    denied = TestClient(make_app(frontend, DenyAll(), config_path=CANARY_CONFIG)).get("/api/info")
    assert denied.status_code == 401
    assert "canary" not in denied.text

    allowed = TestClient(make_app(frontend, AllowAll(), config_path=CANARY_CONFIG)).get("/api/info")
    assert allowed.json()["names"]["source"] == "canary-source-name-7q"


def test_check_detects_a_leak(tmp_path: Path) -> None:
    """Gegenprobe: Stünde ein Wert in einer ausgelieferten Datei, fände der Test ihn."""
    frontend = build_frontend(tmp_path)
    (frontend / "index.html").write_text("<!doctype html><p>Server 10.77.31.41</p>")
    response = TestClient(make_app(frontend, DenyAll(), config_path=CANARY_CONFIG)).get("/")
    body = response.text.lower()
    leaked = [v for v in sensitive_values(load_config(CANARY_CONFIG)) if v.lower() in body]
    assert leaked == ["10.77.31.41"]
