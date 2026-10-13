"""Negativtests für jede Prüfregel der Konfiguration (Konzept 6.3)."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import ConfigError, TwinRaidConfig, load_config

_DELETE = object()


def _example() -> dict[str, Any]:
    # Any: verschachtelte YAML-Daten werden in Tests gezielt verändert.
    data: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    return copy.deepcopy(data)


def _with(dotted: str, value: object) -> dict[str, Any]:
    data = _example()
    *parents, last = dotted.split(".")
    node = data
    for key in parents:
        node = node[key]
    if value is _DELETE:
        del node[last]
    else:
        node[last] = value
    return data


def _load(tmp_path: Path, data: dict[str, Any]) -> TwinRaidConfig:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return load_config(path)


def _problems(tmp_path: Path, data: dict[str, Any]) -> str:
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, data)
    return "\n".join(excinfo.value.problems)


INVALID_CASES: list[tuple[str, object, str]] = [
    # --- Share-Namen: verbotene Shares und Positivliste ---
    ("sync.shares", ["appdata", "system"], "must never be synced"),
    ("sync.shares", ["appdata", "Domains"], "must never be synced"),
    ("sync.shares", ["appdata", "a/b"], "invalid share name"),
    ("sync.shares", ["appdata", ".."], "invalid share name"),
    ("sync.shares", ["appdata", ".hidden"], "invalid share name"),
    ("sync.shares", ["appdata", "-rf"], "invalid share name"),
    ("sync.shares", ["appdata", "my share"], "invalid share name"),
    ("sync.shares", ["appdata", "bücher"], "invalid share name"),
    ("sync.shares", ["appdata", "medien\n"], "invalid share name"),
    ("sync.shares", ["appdata", "a" * 65], "invalid share name"),
    ("sync.shares", ["appdata", ""], "invalid share name"),
    ("sync.shares", ["appdata", 2024], "valid string"),
    ("sync.shares", ["appdata", "appdata"], "duplicates"),
    ("sync.shares", ["appdata", "Appdata"], "duplicates"),
    ("sync.shares", [], "at least 1"),
    ("sync.appdata_share", "medien2", "must be listed in shares"),
    ("sync.appdata_share", "system", "must never be synced"),
    # --- Pfade in exclude ---
    ("sync.exclude", ["/appdata/twinraid"], "no leading '/'"),
    ("sync.exclude", ["appdata/../dokumente"], "'.' or '..'"),
    ("sync.exclude", ["appdata/./twinraid"], "'.' or '..'"),
    ("sync.exclude", ["appdata//twinraid"], "empty parts"),
    ("sync.exclude", ["appdata/twinraid/"], "empty parts"),
    ("sync.exclude", ["appdata/-twinraid"], "starting with '-'"),
    ("sync.exclude", ["-appdata/twinraid"], "starting with '-'"),
    ("sync.exclude", ["appdata/twin\x00raid"], "control or invisible"),
    ("sync.exclude", ["appdata/twin\nraid"], "control or invisible"),
    ("sync.exclude", ["appdata/‮twinraid"], "control or invisible"),
    ("sync.exclude", ["appdata/​twinraid"], "control or invisible"),
    ("sync.exclude", ["appdata/*.tmp"], "wildcard"),
    ("sync.exclude", ["appdata/cache?"], "wildcard"),
    ("sync.exclude", ["appdata/[ab]"], "wildcard"),
    ("sync.exclude", ["appdata\\twinraid"], "wildcard"),
    ("sync.exclude", ["fotos/urlaub"], "must start with one of the configured shares"),
    ("sync.exclude", ["appdata"], "would exclude a whole share"),
    ("sync.exclude", [""], "must not be empty"),
    # --- Pfade in protect_on_target ---
    ("sync.protect_on_target", ["/appdata/_twinraid"], "no leading '/'"),
    ("sync.protect_on_target", ["../appdata"], "'.' or '..'"),
    ("sync.protect_on_target", ["appdata/-x"], "starting with '-'"),
    ("sync.protect_on_target", ["appdata/\x1b[31m"], "control or invisible"),
    ("sync.protect_on_target", ["unbekannt/x"], "must start with one of the configured shares"),
    # --- Container-Namen ---
    ("sync.never_stop", ["bad name"], "invalid container name"),
    ("sync.never_stop", ["-x"], "invalid container name"),
    # --- Zahlen und Schalter ---
    ("sync.max_deletions", -1, "greater than or equal to 0"),
    ("sync.stop_containers", "yes", "valid boolean"),
    ("sync.schedule.enabled", 1, "valid boolean"),
    # --- Cron ---
    ("sync.schedule.cron", "0 3 * *", "invalid cron expression"),
    ("sync.schedule.cron", "61 3 * * 0", "invalid cron expression"),
    ("sync.schedule.cron", "sonntags um drei", "invalid cron expression"),
    # --- Web ---
    ("web.port", 0, "greater than or equal to 1"),
    ("web.port", 70000, "less than or equal to 65535"),
    ("web.port", "8443", "valid integer"),
    ("web.allowed_networks", [], "at least 1"),
    ("web.allowed_networks", ["192.168.10.1/24"], "allowed_networks[0]"),
    ("web.allowed_networks", ["kein-netz"], "allowed_networks[0]"),
    ("web.default_language", "fr", "'de' or 'en'"),
    ("web.session_idle_minutes", 0, "greater than or equal to 1"),
    # --- Server ---
    ("servers.source.host", "999.1.1.1", "servers.source.host"),
    ("servers.source.host", "unraid.local", "servers.source.host"),
    ("servers.source.host", "127.0.0.1", "not a usable host address"),
    ("servers.source.host", "0.0.0.0", "not a usable host address"),  # noqa: S104
    ("servers.target.host", "192.168.10.10", "different host addresses"),
    ("servers.target.mac", "AA:BB:CC:DD:EE", "invalid MAC address"),
    ("servers.target.mac", "AA:BB:CC:DD:EE:FG", "invalid MAC address"),
    ("servers.target.mac", "AA:BB-CC:DD:EE:FF", "invalid MAC address"),
    ("servers.target.mac", "FF:FF:FF:FF:FF:FF", "multicast/broadcast"),
    ("servers.target.mac", "01:00:5E:00:00:01", "multicast/broadcast"),
    ("servers.target.mac", "00:00:00:00:00:00", "must not be 00:00:00:00:00:00"),
    ("servers.target.wol_broadcast", "2001:db8::ff", "valid IPv4 address"),
    ("servers.target.boot_timeout_minutes", 0, "greater than or equal to 1"),
    ("servers.source.name", "", "must not be empty"),
    ("servers.source.name", "Klein\x07", "control or invisible"),
    # --- Gaming-VM ---
    ("gaming_vm.display_name", "   ", "must not be empty"),
    ("gaming_vm.unraid_vm_name", "x" * 129, "longer than 128"),
    ("gaming_vm.unraid_vm_name", "Win‮11", "control or invisible"),
    ("gaming_vm.shutdown_timeout_minutes", 61, "less than or equal to 60"),
    # --- Unbekannte und fehlende Schlüssel ---
    ("web.prot", 8443, "web.prot: unknown key"),
    ("sync.schedule.crn", "0 3 * * 0", "unknown key"),
    ("extra", 1, "extra: unknown key"),
    ("sync.max_deletions", _DELETE, "sync.max_deletions: required key is missing"),
    ("servers.target.mac", _DELETE, "servers.target.mac: required key is missing"),
]


@pytest.mark.parametrize(
    ("dotted", "value", "expected"),
    INVALID_CASES,
    ids=[f"{case[0]}={case[1]!r}"[:60] for case in INVALID_CASES],
)
def test_invalid_values_are_rejected(
    tmp_path: Path, dotted: str, value: object, expected: str
) -> None:
    assert expected in _problems(tmp_path, _with(dotted, value))


def test_error_location_points_to_list_item(tmp_path: Path) -> None:
    problems = _problems(tmp_path, _with("sync.shares", ["appdata", "system"]))
    assert problems.startswith("sync.shares[1]: share 'system' must never be synced")


def test_mac_is_normalized(tmp_path: Path) -> None:
    config = _load(tmp_path, _with("servers.target.mac", "aa-bb-cc-dd-ee-0f"))
    assert config.servers.target.mac == "AA:BB:CC:DD:EE:0F"


def test_ipv6_is_accepted(tmp_path: Path) -> None:
    data = _with("web.allowed_networks", ["192.168.10.0/24", "fd00:10::/64"])
    config = _load(tmp_path, data)
    assert [str(net) for net in config.web.allowed_networks] == [
        "192.168.10.0/24",
        "fd00:10::/64",
    ]


def test_valid_paths_with_spaces_and_umlauts(tmp_path: Path) -> None:
    data = _with("sync.exclude", ["appdata/twinraid", "medien/Meine Bilder/Ü-Ordner"])
    config = _load(tmp_path, data)
    assert config.sync.exclude[1] == "medien/Meine Bilder/Ü-Ordner"


def test_empty_lists_are_allowed(tmp_path: Path) -> None:
    data = _example()
    data["sync"]["exclude"] = []
    data["sync"]["protect_on_target"] = []
    data["sync"]["never_stop"] = []
    config = _load(tmp_path, data)
    assert config.sync.exclude == []


def test_config_is_immutable() -> None:
    config = load_config(EXAMPLE_CONFIG)
    with pytest.raises(ValueError, match="frozen"):
        config.web.port = 1  # type: ignore[misc]
