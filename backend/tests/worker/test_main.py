"""Startverhalten des Workers bei fehlerhafter Umgebung."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.worker.main import EXIT_STARTUP_ERROR, main


@pytest.fixture(autouse=True)
def _valid_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("TWINRAID_WEB_UID", "10001")
    monkeypatch.setenv("TWINRAID_WORKER_SOCKET", str(tmp_path / "w.sock"))


def test_missing_mode_stops_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_MODE")
    assert main() == EXIT_STARTUP_ERROR


def test_real_mode_stops_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "real")
    assert main() == EXIT_STARTUP_ERROR


def test_missing_web_uid_stops_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_WEB_UID")
    assert main() == EXIT_STARTUP_ERROR


def test_invalid_config_stops_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("web: {}\n")
    monkeypatch.setenv("TWINRAID_CONFIG", str(bad))
    assert main() == EXIT_STARTUP_ERROR
