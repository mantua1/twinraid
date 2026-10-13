"""Startverhalten des Web-Prozesses bei fehlerhafter Umgebung."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.web.main import EXIT_STARTUP_ERROR, main


def test_missing_mode_stops_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWINRAID_MODE")
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))
    assert main() == EXIT_STARTUP_ERROR


def test_invalid_config_stops_web(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("web: {}\n")
    monkeypatch.setenv("TWINRAID_CONFIG", str(bad))
    assert main() == EXIT_STARTUP_ERROR
