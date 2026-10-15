"""Eigene Adresse des Containers (Konzept 5.1).

Die IP wird nur im Unraid-Template festgelegt („Fixed IP address“ auf br0).
twinraid-init erkennt sie beim Start und verwendet sie für Zertifikat,
Namensbeschränkung der CA und Host-Prüfung. Ist web.tls.ip_addresses gesetzt,
gilt es statt der Erkennung. Gibt es keine oder mehrere verwendbare Adressen,
startet der Container nicht.
"""

import array
import fcntl
import socket
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv6Address, ip_address, ip_network
from pathlib import Path
from typing import Literal

from twinraid.config.models import TlsConfig

IpAddress = IPv4Address | IPv6Address
AddressOrigin = Literal["detected", "configured"]

# Typische Bereiche für Docker-interne Netze (Bridge). Mit eigener IP auf br0
# liegt die Adresse im Heimnetz.
DOCKER_INTERNAL = ip_network("172.16.0.0/12")

_SIOCGIFCONF = 0x8912
_SIOCGIFNETMASK = 0x891B
# struct ifreq unter Linux (64 Bit): 16 Byte Name, 24 Byte Union; die IPv4-Adresse
# steht in sockaddr_in ab Byte 20.
_IFREQ_SIZE = 40
_MAX_INTERFACES = 128
_IF_INET6 = Path("/proc/net/if_inet6")


class AddressError(Exception):
    """Die eigene Adresse ist nicht eindeutig bestimmbar."""


@dataclass(frozen=True)
class InterfaceAddress:
    interface: str
    address: IpAddress


@dataclass(frozen=True)
class InterfaceNetwork:
    """IPv4-Adresse einer Schnittstelle samt Netzmaske."""

    interface: str
    network: IPv4Interface


@dataclass(frozen=True)
class TlsAddresses:
    """Verwendete Adressen: für Zertifikat, CA und Host-Prüfung in allen Prozessen gleich."""

    ip_addresses: tuple[IpAddress, ...]
    hostnames: tuple[str, ...]
    origin: AddressOrigin
    # Schnittstelle der erkannten Adresse; None bei konfigurierten Adressen.
    interface: str | None = None

    def allowed_hosts(self) -> frozenset[str]:
        """Werte, die im Host-Header stehen dürfen (ohne Port)."""
        return frozenset([*(str(ip) for ip in self.ip_addresses), *self.hostnames])

    def describe(self) -> str:
        ips = ", ".join(str(ip) for ip in self.ip_addresses)
        if self.origin == "detected":
            return f"{ips} (detected on {self.interface})"
        return f"{ips} (configured in web.tls.ip_addresses)"

    def to_json(self) -> dict[str, object]:
        return {
            "ip_addresses": [str(ip) for ip in self.ip_addresses],
            "hostnames": list(self.hostnames),
            "origin": self.origin,
            "interface": self.interface,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> TlsAddresses:
        ips, names = data.get("ip_addresses"), data.get("hostnames")
        origin, interface = data.get("origin"), data.get("interface")
        if not isinstance(ips, list) or not ips or not isinstance(names, list):
            raise ValueError("addresses are missing")
        if origin not in ("detected", "configured"):
            raise ValueError("unknown address origin")
        return cls(
            ip_addresses=tuple(ip_address(str(ip)) for ip in ips),
            hostnames=tuple(str(name) for name in names),
            origin="detected" if origin == "detected" else "configured",
            interface=interface if isinstance(interface, str) else None,
        )


def configured(ip_addresses: Sequence[IpAddress], hostnames: Sequence[str] = ()) -> TlsAddresses:
    return TlsAddresses(tuple(ip_addresses), tuple(hostnames), "configured")


def interface_addresses() -> list[InterfaceAddress]:
    """IPv4-Adressen aller aktiven Schnittstellen im Container (ioctl SIOCGIFCONF)."""
    buffer = array.array("B", bytes(_IFREQ_SIZE * _MAX_INTERFACES))
    address, _ = buffer.buffer_info()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("iL", len(buffer), address)
        result = fcntl.ioctl(sock.fileno(), _SIOCGIFCONF, request)
    length = struct.unpack("iL", result)[0]
    data = buffer.tobytes()[:length]
    found: list[InterfaceAddress] = []
    for offset in range(0, length, _IFREQ_SIZE):
        name = data[offset : offset + 16].split(b"\0", 1)[0].decode(errors="replace")
        raw = data[offset + 20 : offset + 24]
        found.append(InterfaceAddress(name, IPv4Address(raw)))
    return found


def interface_networks() -> list[InterfaceNetwork]:
    """IPv4-Netze der Schnittstellen (ioctl SIOCGIFNETMASK), z. B. für Wake-on-LAN (Konzept 4.2)."""
    found: list[InterfaceNetwork] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for entry in interface_addresses():
            if not isinstance(entry.address, IPv4Address):
                continue
            request = struct.pack("16s24x", entry.interface.encode()[:15])
            try:
                result = fcntl.ioctl(sock.fileno(), _SIOCGIFNETMASK, request)
            except OSError:
                continue
            mask = IPv4Address(result[20:24])
            found.append(
                InterfaceNetwork(entry.interface, IPv4Interface(f"{entry.address}/{mask}"))
            )
    return found


def ipv6_addresses(source: Path = _IF_INET6) -> list[InterfaceAddress]:
    """Globale IPv6-Adressen, nur zur Information im Log (werden nie erkannt)."""
    try:
        lines = source.read_text().splitlines()
    except OSError:
        return []
    found: list[InterfaceAddress] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 6:
            continue
        try:
            address = IPv6Address(int(parts[0], 16))
        except ValueError:
            continue
        if not (address.is_loopback or address.is_link_local or address.is_multicast):
            found.append(InterfaceAddress(parts[5], address))
    return found


def is_usable(address: IpAddress) -> bool:
    """Nur IPv4; nie Loopback, Link-Local, Multicast, 0.0.0.0 oder Broadcast."""
    return (
        isinstance(address, IPv4Address)
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and address != IPv4Address("255.255.255.255")
    )


def _listing(found: Sequence[InterfaceAddress]) -> str:
    return ", ".join(f"{entry.address} on {entry.interface}" for entry in found) or "none"


_HOW_TO_FIX = (
    "Give the container exactly one own IP: in the Unraid template choose Network Type "
    "'Custom: br0' and set 'Fixed IP address'. Alternatively set web.tls.ip_addresses in "
    "twinraid.yaml."
)


def resolve_addresses(
    tls: TlsConfig, discover: Callable[[], list[InterfaceAddress]] | None = None
) -> TlsAddresses:
    """Konfigurierte Adressen oder genau eine erkannte IPv4-Adresse. Wirft AddressError."""
    hostnames = tuple(tls.hostnames)
    if tls.ip_addresses is not None:
        return TlsAddresses(tuple(tls.ip_addresses), hostnames, "configured")
    found = (discover or interface_addresses)()
    usable: dict[IpAddress, InterfaceAddress] = {}
    for entry in found:
        if is_usable(entry.address):
            usable.setdefault(entry.address, entry)
    if len(usable) == 1:
        entry = next(iter(usable.values()))
        return TlsAddresses((entry.address,), hostnames, "detected", entry.interface)
    if not usable:
        raise AddressError(
            f"No usable IPv4 address found for TwinRaid (found: {_listing(found)}). {_HOW_TO_FIX}"
        )
    raise AddressError(
        f"Several usable IPv4 addresses found ({_listing(list(usable.values()))}); TwinRaid "
        f"cannot tell which one clients use. {_HOW_TO_FIX}"
    )


def bridge_warning(addresses: TlsAddresses) -> str | None:
    """Warnung, wenn die erkannte Adresse in einem Docker-internen Netz liegt."""
    if addresses.origin != "detected":
        return None
    inside = [ip for ip in addresses.ip_addresses if ip in DOCKER_INTERNAL]
    if not inside:
        return None
    return (
        f"The detected address {inside[0]} is in {DOCKER_INTERNAL}, typical for Docker's "
        "internal networks. In the Unraid template 'Bridge' is probably selected instead of "
        "'Custom: br0', and clients cannot reach this address. Choose 'Custom: br0' with a "
        "fixed IP, or set web.tls.ip_addresses to the address clients use."
    )
