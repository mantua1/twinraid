"""Negativtests für jede Prüfregel der Konfiguration (Konzept 6.3)."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import EXAMPLE_CONFIG, REPO_ROOT
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
    # --- Entfallene Schlüssel (ab v0.5.1, Konzept 6.3): nicht leere Listen sind ein Fehler ---
    ("sync.exclude", ["appdata/twinraid"], "exclude is no longer supported since v0.5.1"),
    ("sync.exclude", ["appdata/twinraid"], "own dataset below the share"),
    ("sync.protect_on_target", ["appdata/_twinraid"], "protect_on_target is no longer supported"),
    ("sync.protect_on_target", ["x"], "snapshots on the target"),
    ("sync.exclude", "appdata", "valid list"),
    ("sync.appdata_share", 5, "valid string"),
    ("sync.max_deletions", "1000", "valid integer"),
    # --- Container-Namen ---
    ("sync.never_stop", ["bad name"], "invalid container name"),
    ("sync.never_stop", ["-x"], "invalid container name"),
    # --- Zahlen und Schalter ---
    ("sync.stop_containers", "yes", "valid boolean"),
    ("sync.schedule.enabled", 1, "valid boolean"),
    # --- Zeitplan: Wochentage ---
    ("sync.schedule.days", [], "at least 1"),
    ("sync.schedule.days", ["sun", "sun"], "days contains duplicates: sun"),
    ("sync.schedule.days", ["sun", "mon", "sun", "mon"], "days contains duplicates: mon, sun"),
    ("sync.schedule.days", ["sunday"], "'mon', 'tue'"),
    ("sync.schedule.days", ["Sun"], "'mon', 'tue'"),
    ("sync.schedule.days", ["so"], "'mon', 'tue'"),
    ("sync.schedule.days", [0], "'mon', 'tue'"),
    ("sync.schedule.days", "sun", "valid list"),
    # --- Zeitplan: Uhrzeit ---
    ("sync.schedule.time", "3:00", "expected HH:MM"),
    ("sync.schedule.time", "24:00", "expected HH:MM"),
    ("sync.schedule.time", "03:60", "expected HH:MM"),
    ("sync.schedule.time", "03:00:00", "expected HH:MM"),
    ("sync.schedule.time", "0300", "expected HH:MM"),
    ("sync.schedule.time", " 03:00", "expected HH:MM"),
    ("sync.schedule.time", "03:00\n", "expected HH:MM"),
    # Vollbreiten-Ziffern: nur ASCII-Ziffern sind erlaubt.
    ("sync.schedule.time", "\uff10\uff13:\uff10\uff10", "expected HH:MM"),
    ("sync.schedule.time", 180, "must be written in quotes"),
    ("sync.schedule.time", True, "valid string"),
    # --- Zeitplan: Zeitzone ---
    ("sync.schedule.timezone", "Europe/Berlinn", "unknown time zone"),
    ("sync.schedule.timezone", "europe/berlin", "unknown time zone"),
    ("sync.schedule.timezone", "", "unknown time zone"),
    ("sync.schedule.timezone", "localtime", "unknown time zone"),
    ("sync.schedule.timezone", "../../etc/passwd", "unknown time zone"),
    ("sync.schedule.timezone", "MEZ", "unknown time zone"),
    ("sync.schedule.timezone", 1, "valid string"),
    ("sync.schedule.timezone", _DELETE, "sync.schedule.timezone: required key is missing"),
    # --- Web ---
    ("web.port", 0, "greater than or equal to 1"),
    ("web.port", 70000, "less than or equal to 65535"),
    ("web.port", "8443", "valid integer"),
    ("web.allowed_networks", [], "at least 1"),
    ("web.allowed_networks", ["192.168.10.1/24"], "allowed_networks[0]"),
    # Einzelnes Gerät mit falscher Maske gibt nicht still das ganze Netz frei.
    (
        "web.allowed_networks",
        ["192.168.20.0/24", "192.168.10.50/24"],
        "allowed_networks[1]: 192.168.10.50/24 has host bits set. For this single device "
        "write 192.168.10.50/32, for the whole network write 192.168.10.0/24",
    ),
    ("web.allowed_networks", ["fd00:10::5/64"], "write fd00:10::5/128"),
    ("web.allowed_networks", ["kein-netz"], "is neither a network"),
    ("web.default_language", "fr", "'de' or 'en'"),
    ("web.session_idle_minutes", 0, "greater than or equal to 1"),
    ("web.session_idle_minutes", 721, "less than or equal to 720"),
    # --- TLS ---
    ("web.tls.ip_addresses", [], "at least 1"),
    ("web.tls.ip_addresses", ["0.0.0.0"], "cannot be used in a certificate"),  # noqa: S104
    ("web.tls.ip_addresses", ["224.0.0.1"], "cannot be used in a certificate"),
    ("web.tls.ip_addresses", ["twinraid.local"], "web.tls.ip_addresses[0]"),
    ("web.tls.ip_addresses", ["192.168.10.20", "192.168.10.20"], "duplicate entries"),
    ("web.tls.hostnames", ["*.home.arpa"], "invalid hostname"),
    ("web.tls.hostnames", ["-twinraid.home.arpa"], "invalid hostname"),
    ("web.tls.hostnames", ["twinraid..home.arpa"], "invalid hostname"),
    ("web.tls.hostnames", ["twinraid.home.arpa."], "invalid hostname"),
    ("web.tls.hostnames", ["twin raid"], "invalid hostname"),
    ("web.tls.hostnames", ["x" * 64 + ".home.arpa"], "invalid hostname"),
    ("web.tls.hostnames", ["192.168.10.20"], "looks like an IP address"),
    ("web.tls.hostnames", ["bücher.home.arpa"], "invalid hostname"),
    ("web.tls.hostnames", ["Twin.Home.Arpa", "twin.home.arpa"], "duplicate entries"),
    ("web.tls.hostnames", "twinraid.home.arpa", "valid list"),
    ("web.tls.port", 8443, "web.tls.port: unknown key"),
    ("web.tls", _DELETE, "web.tls: required key is missing"),
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
    ("servers.target.ssh_port", 0, "greater than or equal to 1"),
    ("servers.target.ssh_port", 65536, "less than or equal to 65535"),
    ("servers.target.ssh_port", "22", "valid integer"),
    ("servers.source.name", "", "must not be empty"),
    # --- WebGUI ---
    ("servers.target.webgui", _DELETE, "servers.target.webgui: required key is missing"),
    ("servers.source.webgui", {"port": 80}, "servers.source.webgui.scheme: required key"),
    ("servers.target.webgui", {"scheme": "HTTP"}, "'http' or 'https'"),
    ("servers.target.webgui", {"scheme": "ftp"}, "'http' or 'https'"),
    ("servers.target.webgui", {"scheme": "http", "port": 0}, "greater than or equal to 1"),
    ("servers.target.webgui", {"scheme": "http", "port": 65536}, "less than or equal to 65535"),
    ("servers.target.webgui", {"scheme": "http", "port": "80"}, "valid integer"),
    ("servers.target.webgui", {"scheme": "http", "path": "/x"}, "webgui.path: unknown key"),
    ("servers.source.name", "Klein\x07", "control or invisible"),
    # --- Gaming-VM ---
    ("gaming_vm.display_name", "   ", "must not be empty"),
    ("gaming_vm.unraid_vm_name", "x" * 129, "longer than 128"),
    ("gaming_vm.unraid_vm_name", "Win‮11", "control or invisible"),
    ("gaming_vm.shutdown_timeout_minutes", 61, "less than or equal to 60"),
    # --- Aktionen (Konzept 4.2) ---
    ("actions.wake", "yes", "valid boolean"),
    ("actions.vm", 1, "valid boolean"),
    ("actions.shutdown", None, "valid boolean"),
    ("actions.sync", "yes", "valid boolean"),
    ("actions.sync_dry_run", 1, "valid boolean"),
    ("actions.sync_now", True, "actions.sync_now: unknown key"),
    ("actions", ["wake"], "valid dictionary"),
    # --- Unbekannte und fehlende Schlüssel ---
    ("web.prot", 8443, "web.prot: unknown key"),
    ("sync.schedule.cron", "0 3 * * 0", "sync.schedule.cron: unknown key"),
    ("extra", 1, "extra: unknown key"),
    ("sync.stop_containers", _DELETE, "sync.stop_containers: required key is missing"),
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


def test_single_addresses_and_networks_can_be_mixed(tmp_path: Path) -> None:
    """Einzelne Geräte als /32 oder ohne Maske, gemischt mit ganzen Netzen (Konzept 6.3)."""
    data = _with(
        "web.allowed_networks",
        ["192.168.10.50/32", "192.168.10.51", "192.168.20.0/24", "fd00:10::5/128"],
    )
    config = _load(tmp_path, data)
    assert [str(net) for net in config.web.allowed_networks] == [
        "192.168.10.50/32",
        "192.168.10.51/32",
        "192.168.20.0/24",
        "fd00:10::5/128",
    ]


@pytest.mark.parametrize("path", [EXAMPLE_CONFIG, REPO_ROOT / "config.dev.yaml"])
def test_example_has_no_warnings_except_the_schedule(path: Path) -> None:
    """Beispiel- und Entwicklungskonfiguration nutzen keine entfallenen Schlüssel (6.2)."""
    config = load_config(path)
    assert config.sync.appdata_share is None
    assert config.sync.exclude is None
    assert config.warnings() == [
        "sync.schedule.enabled is true, but schedules are carried out only from phase 6 on; "
        "the schedule is not run."
    ]


def test_removed_keys_of_older_configs_only_warn(tmp_path: Path) -> None:
    """Eine Konfiguration aus v0.5.0 mit leeren Listen startet (bis Version 1.0, Konzept 6.3)."""
    data = _example()
    data["sync"].update(
        appdata_share="appdata", max_deletions=1000, exclude=[], protect_on_target=[]
    )
    data["sync"]["schedule"]["enabled"] = False
    config = _load(tmp_path, data)
    assert config.warnings() == [
        "sync.appdata_share is ignored since v0.5.1; remove it.",
        "sync.max_deletions is ignored since v0.5.1; the limit is max_deletions in "
        "source-guard.conf on the source server. Remove it here.",
        "sync.exclude (empty) is ignored since v0.5.1; remove it.",
        "sync.protect_on_target (empty) is ignored since v0.5.1; remove it.",
    ]
    # Auch ein Name, der nicht in shares steht, stoppt den Container nicht mehr.
    data["sync"]["appdata_share"] = "nicht-da"
    assert _load(tmp_path, data).sync.appdata_share == "nicht-da"


def test_empty_lists_are_allowed(tmp_path: Path) -> None:
    data = _example()
    data["sync"]["never_stop"] = []
    data["sync"]["schedule"]["enabled"] = False
    config = _load(tmp_path, data)
    assert config.sync.never_stop == []
    assert config.warnings() == []


def test_sync_actions_default_to_off(tmp_path: Path) -> None:
    data = _example()
    del data["actions"]
    config = _load(tmp_path, data)
    assert (config.actions.sync_dry_run, config.actions.sync) == (False, False)


def test_schedule_from_example() -> None:
    schedule = load_config(EXAMPLE_CONFIG).sync.schedule
    assert schedule.timezone == "Europe/Berlin"
    assert schedule.days == ["sun"]
    assert schedule.time == "03:00"


@pytest.mark.parametrize(
    ("days", "time", "timezone"),
    [
        (["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "00:00", "UTC"),
        (["sat", "wed"], "23:59", "Europe/Vienna"),
        (["fri"], "12:30", "America/New_York"),
    ],
)
def test_valid_schedules(tmp_path: Path, days: list[str], time: str, timezone: str) -> None:
    data = _example()
    data["sync"]["schedule"].update(days=days, time=time, timezone=timezone)
    schedule = _load(tmp_path, data).sync.schedule
    assert (schedule.days, schedule.time, schedule.timezone) == (days, time, timezone)


def test_unquoted_time_in_yaml_gives_helpful_message(tmp_path: Path) -> None:
    # YAML liest 13:30 ohne Anführungszeichen als Zahl 810 (Sexagesimal).
    content = EXAMPLE_CONFIG.read_text(encoding="utf-8").replace('time: "03:00"', "time: 13:30")
    assert "time: 13:30" in content
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert excinfo.value.problems == [
        'sync.schedule.time: time must be written in quotes, e.g. time: "13:30" '
        "(without quotes YAML reads values like 13:30 as a number)"
    ]


def test_config_is_immutable() -> None:
    config = load_config(EXAMPLE_CONFIG)
    with pytest.raises(ValueError, match="frozen"):
        config.web.port = 1  # type: ignore[misc]


def test_session_idle_minutes_defaults_to_ten(tmp_path: Path) -> None:
    config = _load(tmp_path, _with("web.session_idle_minutes", _DELETE))
    assert config.web.session_idle_minutes == 10


@pytest.mark.parametrize(
    ("webgui", "port"),
    [
        ({"scheme": "http"}, 80),
        ({"scheme": "https"}, 443),
        ({"scheme": "https", "port": 8443}, 8443),
        ({"scheme": "http", "port": 8080}, 8080),
    ],
)
def test_webgui_default_ports(tmp_path: Path, webgui: dict[str, object], port: int) -> None:
    """webgui bestimmt nur noch den Port der Online-Prüfung von Groß (ab v0.4.3)."""
    target = _load(tmp_path, _with("servers.target.webgui", webgui)).servers.target
    assert target.webgui.effective_port == port


def test_unraid_vm_name_is_optional(tmp_path: Path) -> None:
    """Ab v0.4.3 steht die VM in power.conf auf Groß; der Name dient nur dem Abgleich."""
    config = _load(tmp_path, _with("gaming_vm.unraid_vm_name", _DELETE))
    assert config.gaming_vm.unraid_vm_name is None


def test_tls_from_example() -> None:
    tls = load_config(EXAMPLE_CONFIG).web.tls
    assert tls.ip_addresses is None
    assert tls.hostnames == []


def test_tls_hostnames_are_optional_and_lowercased(tmp_path: Path) -> None:
    data = _example()
    del data["web"]["tls"]["hostnames"]
    assert _load(tmp_path, data).web.tls.hostnames == []
    data["web"]["tls"]["hostnames"] = ["TwinRaid.Home.Arpa"]
    assert _load(tmp_path, data).web.tls.hostnames == ["twinraid.home.arpa"]


def test_tls_allows_loopback_and_ipv6(tmp_path: Path) -> None:
    data = _with("web.tls.ip_addresses", ["127.0.0.1", "fd00:10::20"])
    tls = _load(tmp_path, data).web.tls
    assert [str(ip) for ip in tls.ip_addresses or []] == ["127.0.0.1", "fd00:10::20"]


def test_ip_addresses_and_port_are_optional() -> None:
    """Die IP steht nur im Unraid-Template; der Port hat einen Standard (Konzept 6.3)."""
    web = load_config(EXAMPLE_CONFIG).web
    assert web.tls.ip_addresses is None
    assert web.port == 8443


def test_actions_are_off_by_default(tmp_path: Path) -> None:
    """Ohne Abschnitt actions ist im Modus real keine Aktion freigeschaltet (Konzept 4.2)."""
    actions = _load(tmp_path, _with("actions", _DELETE)).actions
    assert (actions.wake, actions.vm, actions.shutdown) == (False, False, False)
    assert load_config(EXAMPLE_CONFIG).actions == actions


def test_actions_can_be_enabled_one_by_one(tmp_path: Path) -> None:
    actions = _load(tmp_path, _with("actions", {"vm": True})).actions
    assert (actions.wake, actions.vm, actions.shutdown) == (False, True, False)


def test_wol_broadcast_and_ssh_port_are_optional() -> None:
    """Die Broadcast-Adresse wird ohne Angabe abgeleitet, SSH nutzt Port 22 (Konzept 6.3)."""
    target = load_config(EXAMPLE_CONFIG).servers.target
    assert target.wol_broadcast is None
    assert target.ssh_port == 22


def test_wol_broadcast_can_be_set(tmp_path: Path) -> None:
    target = _load(tmp_path, _with("servers.target.wol_broadcast", "192.168.10.255")).servers.target
    assert str(target.wol_broadcast) == "192.168.10.255"
