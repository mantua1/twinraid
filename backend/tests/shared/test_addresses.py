"""Eigene Adresse des Containers: Erkennung und sicheres Scheitern (Konzept 5.1)."""

from ipaddress import IPv4Address, ip_address
from pathlib import Path

import pytest

from tests.conftest import REAL_INTERFACE_ADDRESSES
from twinraid.config.models import TlsConfig
from twinraid.shared.addresses import (
    AddressError,
    InterfaceAddress,
    TlsAddresses,
    bridge_warning,
    configured,
    ipv6_addresses,
    resolve_addresses,
)

AUTO = TlsConfig(hostnames=["twinraid.home.arpa"])


def _found(*entries: tuple[str, str]) -> list[InterfaceAddress]:
    return [InterfaceAddress(name, ip_address(address)) for name, address in entries]


def _resolve(*entries: tuple[str, str], tls: TlsConfig = AUTO) -> TlsAddresses:
    return resolve_addresses(tls, discover=lambda: _found(*entries))


def test_real_detection_finds_loopback() -> None:
    """Die Abfrage per ioctl funktioniert auf diesem System (Linux)."""
    found = REAL_INTERFACE_ADDRESSES()
    assert InterfaceAddress("lo", IPv4Address("127.0.0.1")) in found


def test_single_address_is_detected() -> None:
    result = _resolve(("lo", "127.0.0.1"), ("eth0", "192.168.10.20"))
    assert result == TlsAddresses(
        (IPv4Address("192.168.10.20"),), ("twinraid.home.arpa",), "detected", "eth0"
    )
    assert result.allowed_hosts() == {"192.168.10.20", "twinraid.home.arpa"}
    assert result.describe() == "192.168.10.20 (detected on eth0)"


def test_configured_addresses_take_precedence() -> None:
    tls = TlsConfig(ip_addresses=[ip_address("127.0.0.1")], hostnames=["localhost"])

    def never() -> list[InterfaceAddress]:
        raise AssertionError("detection must not run")

    result = resolve_addresses(tls, discover=never)
    assert result.origin == "configured"
    assert result.ip_addresses == (IPv4Address("127.0.0.1"),)
    assert result.describe() == "127.0.0.1 (configured in web.tls.ip_addresses)"


@pytest.mark.parametrize(
    "unusable",
    ["127.0.0.1", "127.1.2.3", "169.254.10.20", "0.0.0.0", "224.0.0.1", "255.255.255.255"],  # noqa: S104 - Testwert
)
def test_unusable_addresses_are_never_used(unusable: str) -> None:
    result = _resolve(("lo", "127.0.0.1"), ("x", unusable), ("eth0", "192.168.10.20"))
    assert result.ip_addresses == (IPv4Address("192.168.10.20"),)


def test_no_usable_address_stops_the_start() -> None:
    with pytest.raises(AddressError) as excinfo:
        _resolve(("lo", "127.0.0.1"), ("eth0", "169.254.3.4"))
    message = str(excinfo.value)
    assert "No usable IPv4 address found" in message
    assert "127.0.0.1 on lo, 169.254.3.4 on eth0" in message
    assert "Custom: br0" in message
    assert "web.tls.ip_addresses" in message


def test_no_interfaces_at_all() -> None:
    with pytest.raises(AddressError, match=r"found: none"):
        _resolve()


def test_several_addresses_stop_the_start() -> None:
    with pytest.raises(AddressError) as excinfo:
        _resolve(("lo", "127.0.0.1"), ("eth0", "192.168.10.20"), ("eth1", "172.18.0.5"))
    message = str(excinfo.value)
    assert "Several usable IPv4 addresses found" in message
    assert "192.168.10.20 on eth0, 172.18.0.5 on eth1" in message
    assert "Custom: br0" in message


def test_same_address_twice_is_unambiguous() -> None:
    result = _resolve(("eth0", "192.168.10.20"), ("eth0:1", "192.168.10.20"))
    assert result.ip_addresses == (IPv4Address("192.168.10.20"),)
    assert result.interface == "eth0"


def test_ipv6_is_never_detected() -> None:
    with pytest.raises(AddressError, match="No usable IPv4 address"):
        _resolve(("lo", "127.0.0.1"), ("eth0", "fd00::20"))


def test_ipv6_addresses_are_listed_for_the_log(tmp_path: Path) -> None:
    source = tmp_path / "if_inet6"
    source.write_text(
        "00000000000000000000000000000001 01 80 10 80       lo\n"
        "fe800000000000000000000000000001 02 40 20 80     eth0\n"
        "fd000000000000000000000000000020 02 40 00 80     eth0\n"
        "kaputt\n"
    )
    assert ipv6_addresses(source) == [InterfaceAddress("eth0", ip_address("fd00::20"))]
    assert ipv6_addresses(tmp_path / "missing") == []


@pytest.mark.parametrize(
    ("addresses", "warns"),
    [
        (TlsAddresses((IPv4Address("172.17.0.2"),), (), "detected", "eth0"), True),
        (TlsAddresses((IPv4Address("172.31.255.1"),), (), "detected", "eth0"), True),
        (TlsAddresses((IPv4Address("172.32.0.1"),), (), "detected", "eth0"), False),
        (TlsAddresses((IPv4Address("192.168.10.20"),), (), "detected", "eth0"), False),
        # Ausdrücklich konfiguriert: Bridge-Netze sind dann erlaubt, keine Warnung.
        (configured([IPv4Address("172.17.0.2")]), False),
    ],
)
def test_bridge_warning(addresses: TlsAddresses, warns: bool) -> None:
    warning = bridge_warning(addresses)
    assert (warning is not None) is warns
    if warning is not None:
        assert "'Bridge'" in warning
        assert "'Custom: br0'" in warning


def test_json_round_trip() -> None:
    detected = TlsAddresses((IPv4Address("192.168.10.20"),), ("a.home.arpa",), "detected", "eth0")
    assert TlsAddresses.from_json(detected.to_json()) == detected
    configured_ = configured([ip_address("127.0.0.1"), ip_address("::1")], ["localhost"])
    assert TlsAddresses.from_json(configured_.to_json()) == configured_


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"ip_addresses": [], "hostnames": [], "origin": "detected"},
        {"ip_addresses": ["192.168.10.20"], "hostnames": [], "origin": "geraten"},
        {"ip_addresses": ["kein-ip"], "hostnames": [], "origin": "configured"},
    ],
)
def test_invalid_json_is_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"."):
        TlsAddresses.from_json(data)
