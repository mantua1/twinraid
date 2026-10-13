"""Gemeinsame Test-Fixtures."""

import socket
from ipaddress import ip_address, ip_network
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"
# Konfiguration mit eindeutigen Testwerten, auch vom Container-Starttest genutzt.
CANARY_CONFIG = REPO_ROOT / "docker" / "smoke" / "canary-config.yaml"

# TEST-NET-1 (RFC 5737): wird nie geroutet, darf für Zeitlimit-Tests verwendet werden.
_TEST_NET = ip_network("192.0.2.0/24")


class RealNetworkError(AssertionError):
    """Ein Test wollte eine Verbindung zu einer echten Adresse aufbauen."""


def _allowed(address: object) -> bool:
    if isinstance(address, str | bytes):
        return True  # Unix-Socket
    if not isinstance(address, tuple) or not address:
        return False
    try:
        host = ip_address(str(address[0]))
    except ValueError:
        return False
    return host.is_loopback or host in _TEST_NET


@pytest.fixture(autouse=True)
def _explicit_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests laufen ausdrücklich im Mock-Modus (Konzept 6.4, kein stiller Standard)."""
    monkeypatch.setenv("TWINRAID_MODE", "mock")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Echte Server werden in Tests nie angesprochen (CLAUDE.md, Arbeitsweise)."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: object) -> None:
        if not _allowed(address):
            raise RealNetworkError(f"test tried to connect to {address!r}")
        original_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        if not _allowed(address):
            raise RealNetworkError(f"test tried to connect to {address!r}")
        return original_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
