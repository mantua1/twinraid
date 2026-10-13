"""Hinweis zum CA-Wechsel bis zur Bestätigung und Adressen in /api/tls (Konzept 5.1, 5.2)."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.tls import install_test_runtime
from tests.web.helpers import TEST_ADDRESSES, login, make_app, make_client


def _client(tmp_path: Path, changed: bool = True) -> tuple[TestClient, str]:
    if changed:
        install_test_runtime(
            tmp_path / "run-tls", TEST_ADDRESSES, ca_change_reason="addresses_changed"
        )
    client = make_client(make_app(tmp_path))
    return client, login(client)


def _audit(tmp_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        return [(str(a), str(r)) for a, r in db.execute("SELECT action, result FROM audit_log")]


def test_tls_info_reports_the_detected_address(tmp_path: Path) -> None:
    client, _csrf = _client(tmp_path, changed=False)
    info = client.get("/api/tls").json()
    assert info["addresses"] == {
        "ip_addresses": ["192.168.10.20"],
        "hostnames": [],
        "origin": "detected",
        "interface": "eth0",
    }
    assert info["ca_change"] is None


def test_notice_stays_until_acknowledged(tmp_path: Path) -> None:
    client, csrf = _client(tmp_path)
    for _ in range(2):
        change = client.get("/api/tls").json()["ca_change"]
        assert change is not None
        assert change["reason"] == "addresses_changed"
    response = client.post("/api/tls/ca-change/acknowledge", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json() == {"detail": "acknowledged"}
    assert client.get("/api/tls").json()["ca_change"] is None
    assert ("ca_change_notice", "acknowledged") in _audit(tmp_path)


def test_acknowledgement_survives_a_restart(tmp_path: Path) -> None:
    client, csrf = _client(tmp_path)
    client.post("/api/tls/ca-change/acknowledge", headers={"X-CSRF-Token": csrf})
    # Neuer Web-Prozess mit derselben Datenbank und derselben CA.
    again = make_client(make_app(tmp_path))
    login(again)
    assert again.get("/api/tls").json()["ca_change"] is None


def test_a_later_ca_change_shows_the_notice_again(tmp_path: Path) -> None:
    client, csrf = _client(tmp_path)
    client.post("/api/tls/ca-change/acknowledge", headers={"X-CSRF-Token": csrf})
    install_test_runtime(tmp_path / "run-tls", TEST_ADDRESSES, ca_change_reason="requested")
    change = client.get("/api/tls").json()["ca_change"]
    assert change is not None
    assert change["reason"] == "requested"


def test_acknowledging_twice_is_audited_once(tmp_path: Path) -> None:
    client, csrf = _client(tmp_path)
    for _ in range(2):
        response = client.post("/api/tls/ca-change/acknowledge", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200
    assert _audit(tmp_path).count(("ca_change_notice", "acknowledged")) == 1


def test_acknowledging_needs_the_csrf_token(tmp_path: Path) -> None:
    client, _csrf = _client(tmp_path)
    assert client.post("/api/tls/ca-change/acknowledge").status_code == 403
    assert client.get("/api/tls").json()["ca_change"] is not None


def test_acknowledging_needs_a_session(tmp_path: Path) -> None:
    install_test_runtime(tmp_path / "run-tls", TEST_ADDRESSES, ca_change_reason="expired")
    client = make_client(make_app(tmp_path))
    assert client.post("/api/tls/ca-change/acknowledge").status_code == 401


def test_nothing_to_acknowledge(tmp_path: Path) -> None:
    client, csrf = _client(tmp_path, changed=False)
    response = client.post("/api/tls/ca-change/acknowledge", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json() == {"detail": "nothing_to_acknowledge"}
    assert ("ca_change_notice", "acknowledged") not in _audit(tmp_path)


def test_host_check_uses_the_detected_address(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path))
    assert client.get("/health").status_code == 200
    other = make_client(make_app(tmp_path), base_url="https://192.168.10.21:8443")
    assert other.get("/health").status_code == 400
