"""Wake-on-LAN für Unraid Groß (Konzept 3.4 und 4.2).

Das Magic Packet besteht aus 6-mal 0xFF und 16-mal der MAC-Adresse. Es geht
dreimal per UDP an Port 9 der Broadcast-Adresse des Server-Netzes. Ist
servers.target.wol_broadcast nicht gesetzt, wird die Adresse aus dem Netz
der eigenen Schnittstelle abgeleitet, sofern Groß darin liegt.
"""

import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address

from twinraid.config.models import TargetServerConfig
from twinraid.shared import addresses
from twinraid.shared.addresses import InterfaceNetwork

WOL_PORT = 9
REPEAT = 3


class BroadcastError(Exception):
    """Die Broadcast-Adresse lässt sich nicht ableiten."""


@dataclass(frozen=True)
class WolTarget:
    mac: str
    broadcast: IPv4Address
    # Für das Startlog: konfiguriert oder aus welcher Schnittstelle abgeleitet.
    origin: str


def magic_packet(mac: str) -> bytes:
    """mac im Format AA:BB:CC:DD:EE:FF (so liefert es die Konfiguration)."""
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError("a MAC address has 6 bytes")
    return b"\xff" * 6 + raw * 16


def derive_broadcast(
    target: IPv4Address | IPv6Address, networks: Sequence[InterfaceNetwork]
) -> InterfaceNetwork | None:
    """Die Schnittstelle, in deren Netz Groß liegt (Loopback zählt nicht)."""
    for entry in networks:
        network = entry.network.network
        if network.prefixlen < 31 and not entry.network.ip.is_loopback and target in network:
            return entry
    return None


def resolve_wol_target(
    target: TargetServerConfig,
    networks: Callable[[], list[InterfaceNetwork]] | None = None,
) -> WolTarget:
    """Broadcast-Adresse aus der Konfiguration oder abgeleitet. Wirft BroadcastError."""
    if target.wol_broadcast is not None:
        return WolTarget(target.mac, target.wol_broadcast, "configured in wol_broadcast")
    found = (networks or addresses.interface_networks)()
    entry = derive_broadcast(target.host, found)
    if entry is None:
        listing = ", ".join(f"{e.network} on {e.interface}" for e in found) or "none"
        raise BroadcastError(
            f"cannot derive the Wake-on-LAN broadcast address: {target.host} is not in a network "
            f"of this container ({listing}). Set servers.target.wol_broadcast in twinraid.yaml."
        )
    broadcast = entry.network.network.broadcast_address
    return WolTarget(target.mac, broadcast, f"derived from {entry.network} on {entry.interface}")


def send_magic_packet(
    wol: WolTarget,
    port: int = WOL_PORT,
    repeat: int = REPEAT,
    socket_factory: Callable[[], socket.socket] | None = None,
) -> None:
    """Sendet das Magic Packet. Wirft OSError, wenn das Senden scheitert."""
    packet = magic_packet(wol.mac)
    factory = socket_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    with factory() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            sock.sendto(packet, (str(wol.broadcast), port))
