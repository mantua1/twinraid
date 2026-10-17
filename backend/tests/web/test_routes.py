"""Verhalten der einzelnen Routen mit gültiger Session."""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from fastapi.testclient import TestClient

from tests.conftest import EXAMPLE_CONFIG
from tests.web.helpers import (
    TEST_ADDRESSES,
    FakeClock,
    FakeWorker,
    build_frontend,
    login,
    make_app,
    make_client,
)
from twinraid import __version__
from twinraid.shared.mode import RunMode
from twinraid.shared.tls import certificate_info, install_runtime, record_refresh_failure


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
        "version": __version__,
        "mode": "mock",
        "default_language": "de",
        "names": {"source": "Unraid Klein", "target": "Unraid Groß", "vm": "Gaming-VM"},
        "read_only": False,
        "actions": {"wake": True, "vm": True, "shutdown": True},
        "vm_shutdown_timeout_minutes": 3,
    }


def test_info_in_real_mode_is_read_only(tmp_path: Path) -> None:
    client = make_client(make_app(tmp_path, mode=RunMode.REAL))
    login(client)
    info = client.get("/api/info").json()
    assert info["mode"] == "real"
    assert info["read_only"] is True
    assert info["actions"] == {"wake": False, "vm": False, "shutdown": False}


def test_info_in_real_mode_shows_enabled_actions(tmp_path: Path) -> None:
    """Freischaltung über actions.* (Konzept 4.2): kein Banner „Nur Statusanzeige“ mehr."""
    text = EXAMPLE_CONFIG.read_text().replace("  vm: false ", "  vm: true  ")
    config = tmp_path / "config.yaml"
    config.write_text(text)
    client = make_client(make_app(tmp_path, mode=RunMode.REAL, config_path=config))
    login(client)
    info = client.get("/api/info").json()
    assert info["read_only"] is False
    assert info["actions"] == {"wake": False, "vm": True, "shutdown": False}


def test_status_from_worker(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/status")
    assert response.status_code == 200
    unknown = {"state": "unknown", "reason": None}
    body = response.json()
    assert body.pop("actions")["availability"]["vm_start"] == "target_unknown"
    assert body.pop("other_vms_running") is None
    assert body.pop("notices") == []
    assert body.pop("watch_retry") is None
    assert body == {
        "source": unknown,
        "target": unknown,
        "vm": unknown,
        "checked_at": None,
        "job_running": False,
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
    assert info["source"] == "ca"
    assert info["renewal_failed"] is False
    chain = x509.load_pem_x509_certificates((tmp_path / "run-tls" / "cert.pem").read_bytes())
    server, ca = (certificate_info(c) for c in chain)
    assert info["server"]["fingerprint_sha256"] == server.fingerprint
    assert info["server"]["days_left"] >= 824
    assert info["server"]["expires_soon"] is False
    assert info["ca"]["fingerprint_sha256"] == ca.fingerprint
    assert info["ca"]["fingerprint_sha1"] == ca.fingerprint_sha1
    assert info["ca"]["name"] == ca.name
    assert info["ca"]["days_left"] >= 3649
    assert info["ca"]["expires_soon"] is False


def test_tls_info_reports_failed_renewal(tmp_path: Path) -> None:
    client = _client(tmp_path)
    record_refresh_failure(tmp_path / "run-tls", datetime.now(UTC), os.getgid())
    assert client.get("/api/tls").json()["renewal_failed"] is True


def test_ca_certificate_download(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/tls/ca-certificate")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="twinraid-ca.crt"'
    ca = x509.load_der_x509_certificate(response.content)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert b"PRIVATE KEY" not in response.content


def test_no_ca_download_with_custom_certificate(tmp_path: Path) -> None:
    client = _client(tmp_path)
    runtime = tmp_path / "run-tls"
    chain = (runtime / "cert.pem").read_bytes()
    key = (runtime / "key.pem").read_bytes()
    install_runtime(runtime, chain, key, None, "custom", os.getgid(), TEST_ADDRESSES)
    assert client.get("/api/tls/ca-certificate").status_code == 404
    info = client.get("/api/tls").json()
    assert info["source"] == "custom"
    assert info["ca"] is None


def test_server_certificate_download_is_gone(tmp_path: Path) -> None:
    assert _client(tmp_path).get("/api/tls/certificate").status_code == 404


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
