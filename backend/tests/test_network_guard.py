"""Der Testschutz verhindert Verbindungen zu echten Adressen."""

import asyncio
import socket

import pytest

from tests.conftest import RealNetworkError
from twinraid.integrations.network import tcp_reachable


def test_blocks_real_addresses() -> None:
    with socket.socket() as sock, pytest.raises(RealNetworkError):
        sock.connect(("192.168.10.11", 80))


def test_blocks_real_addresses_in_asyncio() -> None:
    with pytest.raises(RealNetworkError):
        asyncio.run(tcp_reachable("192.168.10.11", 80, wait_seconds=0.5))


def test_allows_loopback() -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        with socket.create_connection(listener.getsockname()):
            pass
    finally:
        listener.close()
