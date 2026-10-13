"""Prüfregeln für einzelne Konfigurationswerte (Konzept 6.3).

Die Werte landen später als Argumente bei rsync und ssh. Deshalb sind die
Regeln bewusst streng und arbeiten mit Positivlisten, wo es möglich ist.
"""

import re
import unicodedata
import zoneinfo
from functools import cache
from ipaddress import IPv4Address, IPv6Address

# Positivliste für Share-Namen: ASCII-Buchstaben, Ziffern, Punkt, Unterstrich,
# Bindestrich; erstes Zeichen Buchstabe oder Ziffer; höchstens 64 Zeichen.
_SHARE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
# Shares, deren Spiegelung mit --delete Groß zerstören würde (Konzept 4.5).
FORBIDDEN_SHARES = frozenset({"system", "domains"})

# Container-Namen wie bei Docker erlaubt.
_CONTAINER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", re.ASCII)

# DNS-Name nach RFC 1123: Labels aus Buchstaben, Ziffern und Bindestrich,
# nicht mit Bindestrich beginnend oder endend, je höchstens 63 Zeichen.
_HOSTNAME_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_MAX_HOSTNAME_LENGTH = 253

_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}", re.ASCII)

# Zeichen, die rsync in Filterregeln als Muster auswertet.
_PATH_PATTERN_CHARS = frozenset("*?[]\\")
_MAX_PATH_LENGTH = 1024
_MAX_DISPLAY_LENGTH = 64
_MAX_VM_NAME_LENGTH = 128

# Unicode-Kategorien für Steuerzeichen, unsichtbare Formatzeichen (z. B.
# Richtungswechsel), Zeilen-/Absatztrenner, Surrogate, private und nicht
# zugewiesene Zeichen.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def has_control_chars(value: str) -> bool:
    return any(unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in value)


def validate_share_name(value: str) -> str:
    if _SHARE_NAME_RE.fullmatch(value) is None:
        raise ValueError(
            f"invalid share name {value!r}: allowed are letters A-Z/a-z, digits, '.', '_' "
            "and '-', starting with a letter or digit, at most 64 characters"
        )
    if value.casefold() in FORBIDDEN_SHARES:
        raise ValueError(
            f"share {value!r} must never be synced: mirroring it would destroy docker.img, "
            "libvirt.img or VM disks on the target"
        )
    return value


def validate_relative_path(value: str) -> str:
    """Pfad relativ zu /mnt/user, z. B. "appdata/twinraid"."""
    if value == "":
        raise ValueError("path must not be empty")
    if len(value) > _MAX_PATH_LENGTH:
        raise ValueError(f"path is longer than {_MAX_PATH_LENGTH} characters")
    if has_control_chars(value):
        raise ValueError(f"path {value!r} contains control or invisible characters")
    if value.startswith("/"):
        raise ValueError(f"path {value!r} must be relative to /mnt/user (no leading '/')")
    if any(ch in _PATH_PATTERN_CHARS for ch in value):
        raise ValueError(
            f"path {value!r} must not contain the wildcard characters * ? [ ] or a backslash"
        )
    for part in value.split("/"):
        if part == "":
            raise ValueError(f"path {value!r} must not contain empty parts ('//' or trailing '/')")
        if part in {".", ".."}:
            raise ValueError(f"path {value!r} must not contain '.' or '..' parts")
        if part.startswith("-"):
            raise ValueError(f"path {value!r} must not contain parts starting with '-'")
    return value


def validate_container_name(value: str) -> str:
    if _CONTAINER_NAME_RE.fullmatch(value) is None:
        raise ValueError(
            f"invalid container name {value!r}: allowed are letters, digits, '_', '.' and '-', "
            "starting with a letter or digit"
        )
    return value


def validate_display_text(value: str) -> str:
    if value.strip() == "":
        raise ValueError("text must not be empty")
    if len(value) > _MAX_DISPLAY_LENGTH:
        raise ValueError(f"text is longer than {_MAX_DISPLAY_LENGTH} characters")
    if has_control_chars(value):
        raise ValueError("text contains control or invisible characters")
    return value


def validate_vm_name(value: str) -> str:
    if value.strip() == "":
        raise ValueError("VM name must not be empty")
    if len(value) > _MAX_VM_NAME_LENGTH:
        raise ValueError(f"VM name is longer than {_MAX_VM_NAME_LENGTH} characters")
    if has_control_chars(value):
        raise ValueError("VM name contains control or invisible characters")
    return value


def normalize_mac(value: str) -> str:
    """Prüft eine Unicast-MAC-Adresse und gibt sie als AA:BB:CC:DD:EE:FF zurück."""
    if _MAC_RE.fullmatch(value) is None:
        raise ValueError(f"invalid MAC address {value!r}: expected format AA:BB:CC:DD:EE:FF")
    octets = [int(part, 16) for part in re.split(r"[:-]", value)]
    if octets[0] & 0x01:
        raise ValueError(f"MAC address {value!r} is a multicast/broadcast address")
    if not any(octets):
        raise ValueError("MAC address must not be 00:00:00:00:00:00")
    return ":".join(f"{octet:02X}" for octet in octets)


def validate_host_address[A: (IPv4Address, IPv6Address)](value: A) -> A:
    if value.is_unspecified or value.is_loopback or value.is_multicast:
        raise ValueError(f"{value} is not a usable host address")
    return value


_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", re.ASCII)
# Einträge der Zeitzonen-Datenbank, die keine echten IANA-Zeitzonen sind.
_NON_IANA_ZONES = frozenset({"localtime", "Factory", "posixrules"})


def reject_unquoted_time(value: object) -> object:
    """YAML liest unquotierte Uhrzeiten wie 13:30 als Zahl (Sexagesimal). Klar darauf hinweisen."""
    if isinstance(value, int) and not isinstance(value, bool):
        raise ValueError(
            'time must be written in quotes, e.g. time: "13:30" '
            "(without quotes YAML reads values like 13:30 as a number)"
        )
    return value


def validate_time_of_day(value: str) -> str:
    if _TIME_RE.fullmatch(value) is None:
        raise ValueError(f"invalid time {value!r}: expected HH:MM between 00:00 and 23:59")
    return value


@cache
def _available_time_zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones()) - _NON_IANA_ZONES


def validate_time_zone(value: str) -> str:
    zones = _available_time_zones()
    if not zones:
        raise ValueError("no time zone database is available on this system")
    if value not in zones:
        raise ValueError(f"unknown time zone {value!r}: use an IANA name like 'Europe/Berlin'")
    return value


def reject_duplicate_days[T: str](days: list[T]) -> list[T]:
    duplicates = sorted({day for day in days if days.count(day) > 1})
    if duplicates:
        raise ValueError(f"days contains duplicates: {', '.join(duplicates)}")
    return days


def validate_hostname(value: str) -> str:
    """DNS-Name nach RFC 1123, ohne Platzhalter, in Kleinbuchstaben gespeichert."""
    name = value.lower()
    if not 0 < len(name) <= _MAX_HOSTNAME_LENGTH:
        raise ValueError(f"invalid hostname {value!r}: must be 1 to 253 characters")
    labels = name.split(".")
    if any(_HOSTNAME_LABEL_RE.fullmatch(label) is None for label in labels):
        raise ValueError(
            f"invalid hostname {value!r}: use letters, digits and '-' in dot-separated "
            "labels, e.g. twinraid.home.arpa"
        )
    if all(label.isdigit() for label in labels):
        raise ValueError(f"hostname {value!r} looks like an IP address; use ip_addresses")
    return name


def validate_tls_address[A: (IPv4Address, IPv6Address)](value: A) -> A:
    """Adresse für das Zertifikat. Loopback ist für die lokale Entwicklung erlaubt."""
    if value.is_unspecified or value.is_multicast:
        raise ValueError(f"{value} cannot be used in a certificate")
    return value


def reject_duplicates[T](values: list[T]) -> list[T]:
    seen: set[T] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen:
            duplicates.append(str(value))
        seen.add(value)
    if duplicates:
        raise ValueError(f"duplicate entries: {', '.join(sorted(set(duplicates)))}")
    return values
