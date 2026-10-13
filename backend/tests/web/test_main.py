"""Startverhalten des Web-Prozesses bei fehlerhafter Umgebung."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.web.main import EXIT_STARTUP_ERROR, main


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("TWINRAID_RUNTIME_TLS_DIR", str(tmp_path / "run-tls"))
    monkeypatch.setenv("TWINRAID_DATA_DIR", str(tmp_path / "data"))


def test_missing_mode_stops_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_MODE")
    assert main() == EXIT_STARTUP_ERROR


def test_invalid_config_stops_web(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("web: {}\n")
    monkeypatch.setenv("TWINRAID_CONFIG", str(bad))
    assert main() == EXIT_STARTUP_ERROR


def test_missing_certificate_stops_web(capsys: pytest.CaptureFixture[str]) -> None:
    assert main() == EXIT_STARTUP_ERROR
    assert "twinraid-init must run first" in capsys.readouterr().err
