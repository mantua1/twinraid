"""Die Anleitungen nennen dieselben Werte wie die Host-Skripte (Konzept 3.7, ab v0.5.1)."""

import hashlib
import re
import textwrap

import pytest

from tests.conftest import REPO_ROOT

SCRIPTS = REPO_ROOT / "host-scripts"
GUARD = (SCRIPTS / "guard.sh").read_text(encoding="utf-8")
TESTDATA = (SCRIPTS / "testdata.sh").read_text(encoding="utf-8")
SETUP = (REPO_ROOT / "docs" / "EINRICHTUNG.md").read_text(encoding="utf-8")
RECOVERY = (REPO_ROOT / "docs" / "WIEDERHERSTELLUNG.md").read_text(encoding="utf-8")
# Stand von v0.5.0 für den Rückweg in 15.2; diese Dateien ändern sich nicht mehr.
ROLLBACK = {
    "guard.sh": "af4afe98fc11b20565194d7c22b293c165ee279efd79932878c31b5c80b42940",
    "source-guard.sh": "ca2f12f98dcae89c524b2f361dc6de5e5c3adfdd240fcbc336dc89a6b7939d3a",
}
# Stand von v0.5.1, den die Abschnitte 3 bis 15 laden (git show v0.5.1:host-scripts/<Datei> |
# sha256sum). Abschnitt 16 lädt den aktuellen Stand; dessen Werte kommen aus den Dateien selbst.
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


def _section(number: int) -> str:
    start = SETUP.index(f"\n## {number}. ")
    return SETUP[start : SETUP.index(f"\n## {number + 1}. ", start)]


def _before_sync() -> str:
    """Abschnitte 1 bis 15 und 17 bis 20 ohne den Rückweg auf 0.5.0: Stand v0.5.1."""
    return SETUP.replace(_rollback_section(), "").replace(_section(16), "")


def _sha256(name: str) -> str:
    return hashlib.sha256((SCRIPTS / name).read_bytes()).hexdigest()


def _blocks(text: str = SETUP) -> list[str]:
    # Kästen in Aufzählungen sind eingerückt; die Einrückung zählt nicht zum Befehl.
    return [
        textwrap.dedent(block)
        for _, block in re.findall(r"^( *)```bash\n(.*?)^\1```", text, flags=re.S | re.M)
    ]


def test_scripts_are_taken_from_the_release_of_each_section() -> None:
    version = re.search(r'^__version__ = "(.+)"$', _init(), flags=re.M)
    assert version is not None
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _section(16))) == {f"v{version[1]}"}
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _before_sync())) == {"v0.5.1"}
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _rollback_section())) == {"v0.5.0"}


@pytest.mark.parametrize("name", sorted(RELEASE_0_5_1))
def test_documented_hashes_are_those_of_the_release(name: str) -> None:
    documented = set(re.findall(rf"([0-9a-f]{{64}})  /tmp/{re.escape(name)}", _before_sync()))
    assert documented == {RELEASE_0_5_1[name]}


@pytest.mark.parametrize("name", ["guard.sh", "source-guard.sh", "testdata.sh"])
def test_section_16_loads_the_current_scripts(name: str) -> None:
    documented = re.findall(rf"([0-9a-f]{{64}})  /tmp/{re.escape(name)}", _section(16))
    assert documented == [_sha256(name)]


@pytest.mark.parametrize("name", ["guard.sh", "source-guard.sh"])
def test_section_16_names_the_header_of_the_current_scripts(name: str) -> None:
    header = (SCRIPTS / name).read_text(encoding="utf-8").splitlines()[1]
    assert f"Die zweite Zeile lautet `{header}`" in _section(16)


def test_section_16_shows_the_memory_margin_of_guard() -> None:
    match = re.search(r"^readonly MEMORY_MARGIN=(\d+)$", GUARD, flags=re.M)
    assert match is not None
    line = GUARD[: match.start()].count("\n") + 1
    assert f"`{line}:readonly MEMORY_MARGIN={match[1]}`" in _section(16)


def test_memory_check_formula_uses_the_margin_of_guard() -> None:
    match = re.search(r"^readonly MEMORY_MARGIN=(\d+)$", GUARD, flags=re.M)
    assert match is not None
    gib = int(match[1]) // 1024**3
    assert int(match[1]) == gib * 1024**3
    assert f"nötig = Max memory in GiB + {gib} GiB" in SETUP
    assert f"`nötig = Max memory in GiB + {gib} GiB`" in _section(16)


@pytest.mark.parametrize(("name", "version"), [("15.1", "0.5.1"), ("16.1", "0.5.2")])
def test_backup_of_the_configuration_is_never_overwritten(name: str, version: str) -> None:
    """Ein zweiter Aufruf darf die Sicherung nicht mit der geänderten Datei überschreiben (F11)."""
    start = SETUP.index(f"### {name} ")
    section = SETUP[start : SETUP.index("\n### ", start + 1)]
    backup = f"twinraid.yaml.vor-{version}"
    block = (
        f"cd /mnt/user/appdata/twinraid/config\n"
        f"[ -e {backup} ] || cp -p twinraid.yaml {backup}\n"
        f"ls -l twinraid.yaml*\n"
    )
    assert f"```bash\n{block}```" in section
    assert re.search(
        rf"> \*\*Diesen Kasten allein einfügen\.\*\*[^\n]*\n\n```bash\n{re.escape(block)}", section
    )
    # Sonst nirgends eine Sicherung, die eine vorhandene überschreiben könnte.
    assert not re.search(r"(?<!\|\| )cp -p twinraid\.yaml twinraid\.yaml\.vor-", SETUP)


def test_every_change_of_the_test_data_stands_alone_with_a_warning() -> None:
    """change zweimal verdoppelt die Zählungen; der Befehl steht deshalb allein in seinem Kasten."""
    with_change = [block for block in _blocks() if "testdata.sh change" in block]
    assert len(with_change) == 3
    assert all(block == "bash /boot/config/twinraid/testdata.sh change\n" for block in with_change)
    warnings = re.findall(
        r"> \*\*Diesen Kasten allein und nur einmal einfügen\.\*\*[^\n]*\n\n *```bash\n"
        r" *bash /boot/config/twinraid/testdata\.sh change\n",
        SETUP,
    )
    assert len(warnings) == 3


def test_poweroff_is_only_sent_while_a_receive_runs() -> None:
    """poweroff steht nur in einer Zeile, die es allein bei receive=busy schickt."""
    line = (
        "SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync"
        " | grep -q 'receive=busy'"
        " && { SSH_ORIGINAL_COMMAND=poweroff bash /boot/config/twinraid/guard.sh power;"
        ' echo "Exit: $?"; }'
        ' || echo "kein Empfang, nichts geschickt"\n'
    )
    # Nur Abschnitt 16; frühere Abschnitte prüfen poweroff mit eigenen Sperren und Ablehnungen.
    with_poweroff = [
        block for block in _blocks(_section(16)) if "SSH_ORIGINAL_COMMAND=poweroff" in block
    ]
    assert with_poweroff == [line]
    before = _section(16)[: _section(16).index(line.rstrip("\n"))]
    assert "> **Diesen Kasten allein einfügen, und nur, während gesendet wird" in before[-900:]


def test_wait_for_the_run_in_test_6c_ends_by_itself() -> None:
    with_restart = [block for block in _blocks() if "docker restart TwinRaid" in block]
    assert len(with_restart) == 1
    assert "timeout 300 sh -c 'until grep -q" in with_restart[0]
    assert '|| echo "kein Lauf geöffnet, nichts neu gestartet"' in with_restart[0]


def test_test_data_are_single_files_whose_sum_is_shown_first() -> None:
    """Keine ganze Share kopieren; die Summe der ausgewählten Dateien vorher zeigen."""
    section = _section(16)
    assert "cp -a" not in section
    copies = [block for block in _blocks() if "/mnt/mainpool/twinraidtest/kopie" in block]
    copies = [block for block in copies if block.startswith("mkdir ")]
    assert len(copies) == 2
    files = "/mnt/user/isos/<Datei1> /mnt/user/isos/<Datei2>"
    assert all(f"cp -p {files} /mnt/mainpool/twinraidtest/kopie" in block for block in copies)
    sum_block = f"du -ch {files}\n"
    assert sum_block in _blocks()
    assert section.index(sum_block) < section.index(copies[0])
    # Die Rate von 2,5 GbE ist nur ein Beispiel, keine Eigenschaft der Server.
    for match in re.finditer(r"298 MiB/s", section):
        assert "Beispiel" in section[max(0, match.start() - 120) : match.start()]


def test_mirror_on_the_target_is_only_verified_in_a_snapshot() -> None:
    """Im eingehängten Spiegel kann eine alte ACL stehen (Konzept 3.7)."""
    for line in re.findall(r"testdata\.sh verify (\S+)", SETUP):
        if "twinraid-mirror" in line:
            assert "/.zfs/snapshot/" in line, line
    assert "auch im Spiegel auf Groß" not in SETUP


def test_zfs_destroy_and_rollback_in_the_guide_are_never_recursive() -> None:
    assert not re.search(r"zfs (destroy|rollback) (-\w*[rRf]|.* -[rRf])", SETUP)


def test_section_16_only_names_test_data_and_relative_sizes() -> None:
    """Im Repo nur Testnamen (CLAUDE.md, „Namen der echten Server“)."""
    section = _section(16)
    datasets = set(re.findall(r"mainpool/([A-Za-z0-9._-]+)", section))
    assert datasets <= {"twinraidtest", "twinraid-mirror"}


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
