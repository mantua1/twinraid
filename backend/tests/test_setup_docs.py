"""Die Anleitungen nennen dieselben Werte wie die Host-Skripte (Konzept 3.7, ab v0.5.1)."""

import re

import pytest

from tests.conftest import REPO_ROOT

GUARD = (REPO_ROOT / "host-scripts" / "guard.sh").read_text(encoding="utf-8")
TESTDATA = (REPO_ROOT / "host-scripts" / "testdata.sh").read_text(encoding="utf-8")
SETUP = (REPO_ROOT / "docs" / "EINRICHTUNG.md").read_text(encoding="utf-8")
RECOVERY = (REPO_ROOT / "docs" / "WIEDERHERSTELLUNG.md").read_text(encoding="utf-8")
# Stand von v0.5.0 für den Rückweg in 15.2; diese Dateien ändern sich nicht mehr.
ROLLBACK = {
    "guard.sh": "af4afe98fc11b20565194d7c22b293c165ee279efd79932878c31b5c80b42940",
    "source-guard.sh": "ca2f12f98dcae89c524b2f361dc6de5e5c3adfdd240fcbc336dc89a6b7939d3a",
}
# Stand von v0.5.1, den Abschnitt 15 lädt (git show v0.5.1:host-scripts/<Datei> | sha256sum).
# Die Skripte ändern sich mit v0.5.2; deren Werte prüft der Test für Abschnitt 16.
RELEASE_0_5_1 = {
    "guard.sh": "c5f69807b76eb74af373361c47ecad3e39e718c278df10edba0e2bad40cae47f",
    "source-guard.sh": "d4f05ada883386b365520296312f916fc57d0a283725c0b3c8d5d3ce0f85d647",
    "testdata.sh": "ca4b8833cfde011242f08cf67cd72d5fd7a08e14f3f3db3811076888f864a019",
}


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


def _rollback_section() -> str:
    start = SETUP.index("**Scheitert G10, zurück auf 0.5.0.**")
    return SETUP[start : SETUP.index("### 15.3 Test K1")]


def _without_rollback() -> str:
    return SETUP.replace(_rollback_section(), "")


def test_scripts_are_taken_from_the_current_release() -> None:
    version = re.search(r'^__version__ = "(.+)"$', _init(), flags=re.M)
    assert version is not None
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _without_rollback())) == {
        f"v{version[1]}"
    }
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _rollback_section())) == {"v0.5.0"}


@pytest.mark.parametrize("name", sorted(RELEASE_0_5_1))
def test_documented_hashes_are_those_of_the_release(name: str) -> None:
    documented = set(re.findall(rf"([0-9a-f]{{64}})  /tmp/{re.escape(name)}", _without_rollback()))
    assert documented == {RELEASE_0_5_1[name]}


@pytest.mark.parametrize("name", sorted(ROLLBACK))
def test_rollback_hashes_are_those_of_the_old_version(name: str) -> None:
    documented = set(re.findall(rf"([0-9a-f]{{64}})  /tmp/{re.escape(name)}", _rollback_section()))
    assert documented == {ROLLBACK[name]}


def test_scripts_are_never_loaded_straight_into_boot() -> None:
    assert not re.search(r"wget -O /boot/config/twinraid/\S+\.sh", SETUP)
    downloads = re.findall(r"wget -O /tmp/(guard\.sh|source-guard\.sh|testdata\.sh)", SETUP)
    assert len(downloads) >= 7
    for name in set(downloads):
        count = downloads.count(name)
        escaped = re.escape(name)
        assert len(re.findall(rf"sha256sum /tmp/{escaped}\n", SETUP)) == count
        assert SETUP.count(f"cp /tmp/{name} /boot/config/twinraid/{name}.neu\n") == count
        assert (
            SETUP.count(f"mv /boot/config/twinraid/{name}.neu /boot/config/twinraid/{name}\n")
            == count
        )


def test_lock_of_test_2b_can_be_released() -> None:
    """Ohne -o erbt sleep die Sperre und hält sie nach kill weiter."""
    start = SETUP.index("### 15.7 Test 2b")
    section = SETUP[start : SETUP.index("### 15.8 Test 2c")]
    assert "flock -o /var/run/twinraid-guard-receive.lock sleep 600 &\nLOCKPID=$!\n" in section
    assert "kill $LOCKPID" in section
    assert "`receive=idle`" in section


def _init() -> str:
    return (REPO_ROOT / "backend" / "src" / "twinraid" / "__init__.py").read_text(encoding="utf-8")
