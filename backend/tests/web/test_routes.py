"""Verhalten der einzelnen Routen."""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.web.helpers import AllowAll, FakeWorker, build_frontend, make_app


def _client(tmp_path: Path, worker: FakeWorker | None = None) -> TestClient:
    return TestClient(make_app(build_frontend(tmp_path), AllowAll(), worker))


def test_health_is_plain_ok(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/health")
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
    response = _client(tmp_path, FakeWorker(available=False)).get("/api/status")
    assert response.status_code == 503
    assert response.json() == {"detail": "worker_unavailable"}


def test_index_is_served_without_caching(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"


def test_missing_frontend_gives_404(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path / "missing", AllowAll()))
    assert client.get("/").status_code == 404
    assert client.get("/assets/app.js").status_code == 404


def test_asset_path_traversal_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("geheim")
    client = _client(tmp_path)
    for path in ["/assets/../../secret.txt", "/assets/..%2F..%2Fsecret.txt"]:
        response = client.get(path)
        assert "geheim" not in response.text


def test_unknown_path_is_404_with_session(tmp_path: Path) -> None:
    assert _client(tmp_path).get("/nope").status_code == 404
