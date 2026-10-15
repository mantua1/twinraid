"""Gemeinsame Test-Fixtures."""

import logging
import socket
from collections.abc import Callable, Iterator
from ipaddress import IPv4Interface, ip_address, ip_network
from pathlib import Path

import pytest

from twinraid.shared import addresses

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


# Die echte Erkennung, bevor _fixed_interfaces sie in jedem Test ersetzt.
REAL_INTERFACE_ADDRESSES = addresses.interface_addresses

# Simulierte Netzwerkschnittstellen des Containers: Loopback und genau eine eigene
# Adresse, passend zu config.example.yaml (Client-Netz, BASE_URL der Web-Tests).
TEST_INTERFACES = [
    ("lo", "127.0.0.1/8"),
    ("eth0", "192.168.10.20/24"),
]


@pytest.fixture(autouse=True)
def _fixed_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests hängen nie von den echten Schnittstellen des Rechners ab."""
    networks = [
        addresses.InterfaceNetwork(name, IPv4Interface(value)) for name, value in TEST_INTERFACES
    ]
    found = [addresses.InterfaceAddress(entry.interface, entry.network.ip) for entry in networks]
    monkeypatch.setattr(addresses, "interface_addresses", lambda: list(found))
    monkeypatch.setattr(addresses, "interface_networks", lambda: list(networks))
    monkeypatch.setattr(addresses, "ipv6_addresses", lambda *_args: [])


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

    # Callable[..., int]: socket.sendto hat Überladungen mit und ohne flags.
    original_sendto: Callable[..., int] = socket.socket.sendto

    def sendto(self: socket.socket, data: bytes, *args: object) -> int:
        # Auch UDP (z. B. Wake-on-LAN) nur an Loopback oder TEST-NET-1.
        address = args[-1]
        if not _allowed(address):
            raise RealNetworkError(f"test tried to send to {address!r}")
        return original_sendto(self, data, *args)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Entfernt nach jedem Test die Logging-Ausgabe, die main() eingerichtet hat.

    configure_logging() hängt einen StreamHandler auf die Ausgabe des laufenden
    Tests an. Pytest schließt diese Ausgabe danach; spätere Tests schrieben sonst in
    eine geschlossene Datei ("Logging error"). Die eigenen Handler von pytest sind
    Unterklassen und bleiben unberührt.
    """
    root = logging.getLogger()
    before, level = set(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if type(handler) is logging.StreamHandler and handler not in before:
            root.removeHandler(handler)
    root.setLevel(level)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
