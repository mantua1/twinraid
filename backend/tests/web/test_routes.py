"""Verhalten der einzelnen Routen mit gültiger Session."""

import sqlite3
from pathlib import Path

from cryptography import x509
from fastapi.testclient import TestClient

from tests.web.helpers import FakeClock, FakeWorker, build_frontend, login, make_app, make_client
from twinraid.shared.tls import certificate_info


def _client(tmp_path: Path, worker: FakeWorker | None = None) -> TestClient:
    client = make_client(make_app(tmp_path, worker))
    login(client)
    return client


def test_health_is_plain_ok(tmp_path: Path) -> None:
    response = make_client(make_app(tmp_path)).get("/health")
    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["content-type"].startswith("text/plain")


def test_info_contains_mode_and_names(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/info")
    assert response.status_code == 200
    assert response.json() == {
        "version": "0.1.0",
        "mode": "mock",
        "default_language": "de",
        "names": {"source": "Unraid Klein", "target": "Unraid Groß", "vm": "Gaming-VM"},
    }


def test_status_from_worker(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/status")
    assert response.status_code == 200
    assert response.json() == {
        "source": "unknown",
        "target": "unknown",
        "vm": "unknown",
        "checked_at": None,
        "password_set": True,
    }


def test_status_when_worker_is_down(tmp_path: Path) -> None:
    worker = FakeWorker()
    client = _client(tmp_path, worker)
    worker.available = False
    response = client.get("/api/status")
    assert response.status_code == 503
    assert response.json() == {"detail": "worker_unavailable"}


def test_index_is_served_without_caching(tmp_path: Path) -> None:
    response = make_client(make_app(tmp_path)).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"


def test_missing_frontend_gives_404(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path, frontend_dir=tmp_path / "missing"))
    assert client.get("/").status_code == 404
    assert client.get("/assets/app.js").status_code == 404


def test_asset_path_traversal_is_blocked(tmp_path: Path) -> None:
    build_frontend(tmp_path)
    (tmp_path / "secret.txt").write_text("geheim")
    client = make_client(make_app(tmp_path))
    for path in ["/assets/../../secret.txt", "/assets/..%2F..%2Fsecret.txt"]:
        assert "geheim" not in client.get(path).text


def test_unknown_path_is_404_with_session(tmp_path: Path) -> None:
    assert _client(tmp_path).get("/nope").status_code == 404


def test_tls_info(tmp_path: Path) -> None:
    client = _client(tmp_path)
    info = client.get("/api/tls").json()
    assert info["source"] == "self-signed"
    assert info["days_left"] >= 824
    assert info["expires_soon"] is False
    pem = (tmp_path / "run-tls" / "cert.pem").read_bytes()
    expected = certificate_info(x509.load_pem_x509_certificate(pem)).fingerprint
    assert info["fingerprint_sha256"] == expected


def test_certificate_download(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/tls/certificate")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-pem-file"
    assert response.headers["content-disposition"] == 'attachment; filename="twinraid.crt"'
    assert response.content.startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"PRIVATE KEY" not in response.content


def test_audit_list(tmp_path: Path) -> None:
    entries = _client(tmp_path).get("/api/audit").json()["entries"]
    assert entries[0]["action"] == "login"
    assert entries[0]["result"] == "success"
    assert entries[0]["client_ip"] == "192.168.20.5"


def test_lifespan_purges_expired_data(tmp_path: Path) -> None:
    app = make_app(tmp_path, clock=FakeClock())
    tables = ("sessions", "lockouts", "audit_log")
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        db.execute("INSERT INTO sessions VALUES ('alt', 'x', 0, 0)")
        db.execute("INSERT INTO lockouts VALUES ('10.0.0.1', 0)")
        db.execute(
            "INSERT INTO audit_log (at, client_ip, action, result) "
            "VALUES (0, NULL, 'login', 'success')"
        )
    with make_client(app):
        pass
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608 - feste Tabellennamen
    assert counts == dict.fromkeys(tables, 0)
