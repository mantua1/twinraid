"""Die Anleitungen nennen dieselben Werte wie die Host-Skripte (Konzept 3.7, ab v0.5.1)."""

import re

from tests.conftest import REPO_ROOT

GUARD = (REPO_ROOT / "host-scripts" / "guard.sh").read_text(encoding="utf-8")
TESTDATA = (REPO_ROOT / "host-scripts" / "testdata.sh").read_text(encoding="utf-8")
SETUP = (REPO_ROOT / "docs" / "EINRICHTUNG.md").read_text(encoding="utf-8")
RECOVERY = (REPO_ROOT / "docs" / "WIEDERHERSTELLUNG.md").read_text(encoding="utf-8")


def _recv_perms() -> str:
    match = re.search(r"^readonly RECV_PERMS=(\S+)$", GUARD, flags=re.M)
    assert match is not None
    return match[1]


def _fixed_values() -> list[str]:
    match = re.search(r"^readonly RECEIVE_OPTIONS=\((.*?)^\)$", GUARD, flags=re.M | re.S)
    assert match is not None
    return re.findall(r"-o (\S+)", match[1])


def _constant(name: str) -> int:
    match = re.search(rf"^readonly {name}=(\d+)$", TESTDATA, flags=re.M)
    assert match is not None
    return int(match[1])


def test_delegation_command_grants_exactly_the_rights_of_guard() -> None:
    assert f"zfs allow -u 64000 {_recv_perms()} mainpool/twinraid-mirror\n" in SETUP
    assert len(_recv_perms().split(",")) == 28


def test_expected_delegation_output_is_what_guard_checks() -> None:
    header = "---- Permissions on mainpool/twinraid-mirror ".ljust(70, "-")
    expected = f"{header}\nLocal+Descendent permissions:\n\tuser (unknown: 64000) {_recv_perms()}\n"
    assert expected in SETUP


def test_reset_command_sets_every_fixed_value_except_canmount() -> None:
    match = re.search(r"^zfs set (readonly=on .*) \"\$D\"$", RECOVERY, flags=re.M)
    assert match is not None
    values = match[1].split()
    expected = [value for value in _fixed_values() if not value.startswith("canmount=")]
    assert values == [*expected, "mountpoint=/mnt/mainpool/twinraid-mirror/$N"]


def test_check_of_the_fixed_values_lists_every_property() -> None:
    match = re.search(r"^zfs get -H -o property,value,source (\S+) \"\$D\"$", RECOVERY, flags=re.M)
    assert match is not None
    properties = set(match[1].split(","))
    assert properties == {value.split("=")[0] for value in _fixed_values()} | {"mountpoint"}


def test_table_of_the_fixed_values() -> None:
    for value in _fixed_values():
        name, wanted = value.split("=")
        if name in ("quota", "refquota", "reservation", "refreservation", "sharesmb", "sharenfs"):
            assert f"`{name}`" in RECOVERY
            continue
        assert f"`{name}` | `{wanted}`" in RECOVERY


def test_recovery_never_uses_recursive_or_forced_commands() -> None:
    assert not re.search(r"zfs (destroy|rollback) (-\w*[rRf]|.* -[rRf])", RECOVERY)
    assert "zfs receive" not in RECOVERY


def test_change_numbers_match_the_script() -> None:
    expected = (
        f"{_constant('CHANGE_DELETE')} Dateien gelöscht, {_constant('CHANGE_MODIFY')} geändert, "
        f"{_constant('CHANGE_ADD')} neu, {_constant('CHANGE_RENAME')} umbenannt"
    )
    assert expected in SETUP


def test_new_keys_block_the_start_as_the_guide_says() -> None:
    """Die Anleitung zitiert die Meldungen aus config/models.py."""
    models = (REPO_ROOT / "backend" / "src" / "twinraid" / "config" / "models.py").read_text(
        encoding="utf-8"
    )
    for text in (
        "exclude is no longer supported since v0.5.1",
        "protect_on_target is no longer supported since v0.5.1",
    ):
        assert text in models
        assert text in SETUP


def test_scripts_are_taken_from_the_current_release() -> None:
    version = re.search(r'^__version__ = "(.+)"$', _init(), flags=re.M)
    assert version is not None
    # Nur der Rückweg in 15.2 nennt v0.5.0 ohne Pfad zu einem Skript.
    tags = set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", SETUP))
    assert tags == {f"v{version[1]}"}


def _init() -> str:
    return (REPO_ROOT / "backend" / "src" / "twinraid" / "__init__.py").read_text(encoding="utf-8")
