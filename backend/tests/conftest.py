"""Gemeinsame Test-Fixtures."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"
# Konfiguration mit eindeutigen Testwerten, auch vom Container-Starttest genutzt.
CANARY_CONFIG = REPO_ROOT / "docker" / "smoke" / "canary-config.yaml"


@pytest.fixture(autouse=True)
def _explicit_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests laufen ausdrücklich im Mock-Modus (Konzept 6.4, kein stiller Standard)."""
    monkeypatch.setenv("TWINRAID_MODE", "mock")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
