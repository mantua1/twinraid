"""Startprüfung: Container startet nur mit gültigem Modus und gültiger Konfiguration."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.preflight import main


@pytest.fixture(autouse=True)
def _example_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWINRAID_CONFIG", str(EXAMPLE_CONFIG))


def test_passes_with_mock_and_valid_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main() == 0
    err = capsys.readouterr().err
    assert "SIMULATION MODE ACTIVE" in err
    assert "preflight checks passed" in err


def test_fails_without_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TWINRAID_MODE")
    assert main() == 1
    err = capsys.readouterr().err
    assert "TWINRAID_MODE is not set" in err
    assert "will not start" in err


def test_fails_with_invalid_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWINRAID_MODE", "produktion")
    assert main() == 1


def test_fails_with_invalid_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(EXAMPLE_CONFIG.read_text().replace("- medien", "- system"))
    monkeypatch.setenv("TWINRAID_CONFIG", str(bad))
    assert main() == 1
    assert "sync.shares[2]: share 'system' must never be synced" in capsys.readouterr().err
