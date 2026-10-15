"""Wake-on-LAN: Magic Packet und Broadcast-Adresse (Konzept 4.2)."""

import socket
from ipaddress import IPv4Address, IPv4Interface, ip_address

import pytest

from tests.conftest import EXAMPLE_CONFIG, RealNetworkError
from twinraid.config import load_config
from twinraid.integrations.wol import (
    BroadcastError,
    WolTarget,
    derive_broadcast,
    magic_packet,
    resolve_wol_target,
    send_magic_packet,
)
from twinraid.shared.addresses import InterfaceNetwork

TARGET = load_config(EXAMPLE_CONFIG).servers.target


def test_magic_packet_layout() -> None:
    packet = magic_packet("AA:BB:CC:DD:EE:0F")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddee0f") * 16


def test_broadcast_is_derived_from_the_own_network() -> None:
    # Testschnittstelle eth0 192.168.10.20/24 (tests/conftest.py), Groß 192.168.10.11.
    wol = resolve_wol_target(TARGET)
    assert wol.broadcast == IPv4Address("192.168.10.255")
    assert wol.mac == "AA:BB:CC:DD:EE:FF"
    assert wol.origin == "derived from 192.168.10.20/24 on eth0"


def test_configured_broadcast_wins() -> None:
    target = TARGET.model_copy(update={"wol_broadcast": IPv4Address("10.0.0.255")})
    wol = resolve_wol_target(target)
    assert wol.broadcast == IPv4Address("10.0.0.255")
    assert wol.origin == "configured in wol_broadcast"


def test_target_in_another_network_needs_the_setting() -> None:
    target = TARGET.model_copy(update={"host": ip_address("10.1.2.3")})
    with pytest.raises(BroadcastError, match=r"Set servers\.target\.wol_broadcast") as excinfo:
        resolve_wol_target(target)
    assert "192.168.10.20/24 on eth0" in str(excinfo.value)


@pytest.mark.parametrize(
    ("networks", "expected"),
    [
        ([("lo", "127.0.0.1/8"), ("eth0", "192.168.10.20/24")], "192.168.10.255"),
        ([("eth0", "192.168.10.20/23")], "192.168.11.255"),
        ([("eth0", "192.168.10.20/32")], None),
        ([("lo", "127.0.0.1/8")], None),
    ],
)
def test_derive_broadcast(networks: list[tuple[str, str]], expected: str | None) -> None:
    found = [InterfaceNetwork(name, IPv4Interface(value)) for name, value in networks]
    entry = derive_broadcast(ip_address("192.168.10.11"), found)
    if expected is None:
        assert entry is None
    else:
        assert entry is not None
        assert str(entry.network.network.broadcast_address) == expected


def test_packet_is_sent_three_times() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        port = receiver.getsockname()[1]
        send_magic_packet(WolTarget("AA:BB:CC:DD:EE:FF", IPv4Address("127.0.0.1"), "test"), port)
        packets = [receiver.recv(200) for _ in range(3)]
    assert packets == [magic_packet("AA:BB:CC:DD:EE:FF")] * 3


def test_broadcast_option_is_set() -> None:
    options: list[tuple[int, int, int]] = []

    class Recorder(socket.socket):
        def setsockopt(self, level: int, name: int, value: object) -> None:  # type: ignore[override]
            options.append((level, name, int(value)))  # type: ignore[call-overload]
            super().setsockopt(level, name, value)  # type: ignore[call-overload]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        send_magic_packet(
            WolTarget("AA:BB:CC:DD:EE:FF", IPv4Address("127.0.0.1"), "test"),
            receiver.getsockname()[1],
            repeat=1,
            socket_factory=lambda: Recorder(socket.AF_INET, socket.SOCK_DGRAM),
        )
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in options


def test_tests_never_send_to_real_networks() -> None:
    with pytest.raises(RealNetworkError):
        send_magic_packet(WolTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.168.10.255"), "x"))
