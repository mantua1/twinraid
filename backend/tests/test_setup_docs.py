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
# Abschnitt 16 gilt für v0.5.2; ab v0.5.3 ändern sich source-guard.sh und testdata.sh. Den Stand
# von v0.5.2 lädt auch der Rückweg in 17.13.
RELEASE_0_5_2 = {
    "guard.sh": "30713f387695c48611bcd3762bfca59e1928c5436db0ea8cf7eb1b68143b2e0c",
    "source-guard.sh": "2cbccdfba70f8372799512f1b75e568abd71140c03f4d582af90b99294db0bd7",
    "testdata.sh": "c151d3421085cf2857640377d98ed0b7520c340adc950b6bac6ba725449d39de",
}
HEADERS_0_5_2 = {
    "guard.sh": "# TwinRaid: SSH-Wächter auf Unraid Groß, Version 3 (Konzept 3.5, 3.7 und 5.6).",
    "source-guard.sh": (
        "# TwinRaid: Wächter auf dem Host von Unraid Klein, Version 3 (Konzept 3.6)."
    ),
}

# Ab Abschnitt 17 erkennen Kästen den Server ohne Hostnamen, an der Ablage der Host-Skripte.
ON_KLEIN = "[ -f /boot/config/twinraid/source-guard.sh ] && [ ! -f /boot/config/twinraid/guard.sh ]"
ON_GROSS = "[ -f /boot/config/twinraid/guard.sh ] && [ ! -f /boot/config/twinraid/source-guard.sh ]"
NOT_GROSS = "Das ist nicht Groß. Diesen Kasten im Unraid-Terminal von Groß einfügen."


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


def _recovery_section(title: str) -> str:
    start = RECOVERY.index(f"\n## {title}\n")
    end = RECOVERY.find("\n## ", start + 1)
    return RECOVERY[start : end if end != -1 else len(RECOVERY)]


def test_receive_with_force_only_on_the_way_back_to_klein() -> None:
    """Einzige Stelle mit zfs receive -F (CLAUDE.md, Regel 7): von Hand, über klein nach Klein."""
    lines = [
        line
        for block in _blocks(RECOVERY)
        for line in block.splitlines()
        if "zfs receive" in line or " -F" in line
    ]
    assert lines == [
        '  zfs send -L -c -e -i "mainpool/$N#$B" "mainpool/$N@$R"'
        ' | klein zfs receive -F "mainpool/$N" && echo "Rückweg gesendet"'
    ]
    assert lines[0] in _recovery_section("Rückweg nach Klein")
    # Die Basis wird unmittelbar vorher auf beiden Seiten verglichen.
    block = next(b for b in _blocks(RECOVERY) if "zfs receive -F" in b)
    assert '[ "$GB" = "$GK" ]' in block


def test_klein_is_only_reached_through_one_line() -> None:
    """recovery-test.sh ersetzt genau diese Stelle durch eine lokale Pipe (Konzept 5.6)."""
    assert RECOVERY.count("ssh root@<IP von Klein>") == 1
    assert 'klein() { ssh root@<IP von Klein> "$@"; }\n' in _blocks(RECOVERY)[0]
    tested = "".join(
        _recovery_section(title)
        for title in (
            "Letzten Stand finden",
            "Notbetrieb starten",
            "Rückweg nach Klein",
            "Aufräumen auf Groß",
        )
    )
    for block in _blocks(tested):
        assert not re.search(r"<[A-Za-zÄÖÜäöü]", block), block


def test_clone_gets_the_properties_of_the_concept() -> None:
    """Konzept 4.6: Klon mit ausdrücklich gesetzten Eigenschaften."""
    concept = (REPO_ROOT / "docs" / "KONZEPT.md").read_text(encoding="utf-8")
    match = re.search(r"mit ausdrücklich gesetzten Eigenschaften \(([^)]*)\)", concept)
    assert match is not None
    expected = re.findall(r"`([a-z]+=[a-z0-9]+)`", match[1])
    pattern = r"^zfs clone (.*) \"mainpool/twinraid-mirror/\$N@\$B\" \"mainpool/\$N\"$"
    clone = re.search(pattern, RECOVERY, flags=re.M)
    assert clone is not None
    assert re.findall(r"-o (\S+)", clone[1]) == expected


def test_locked_mirror_keeps_its_anchors() -> None:
    """EINRICHTUNG und die Oberfläche verweisen auf „Spiegel gesperrt“ und seine Unterabschnitte."""
    headings = re.findall(r"^#{2,3} (.+)$", RECOVERY, flags=re.M)
    start = headings.index("Spiegel gesperrt")
    assert headings[start : start + 5] == [
        "Spiegel gesperrt",
        "Woran du es erkennst",
        "Vorbereitung",
        "Nach `received-properties` oder `props-mismatch`",
        "Nach `props-not-applied`",
    ]
    assert len(headings) == len(set(headings))


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
    """Abschnitte 1 bis 15 und 18 bis 21 ohne den Rückweg auf 0.5.0: Stand v0.5.1."""
    return (
        SETUP.replace(_rollback_section(), "").replace(_section(16), "").replace(_section(17), "")
    )


def _rollback_0_5_2() -> str:
    """17.13: zurück auf 0.5.2, mit source-guard.sh aus v0.5.2."""
    section = _section(17)
    start = section.index("### 17.13 ")
    return section[start : section.index("\n### ", start + 1)]


def _section_17_update() -> str:
    """Abschnitt 17 ohne den Rückweg auf 0.5.2: Stand v0.5.3."""
    return _section(17).replace(_rollback_0_5_2(), "")


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
    # Abschnitt 16 bleibt bei v0.5.2, nur testdata.sh kommt aus v0.5.3 (bulk, F15).
    section_16 = re.findall(r"mantua1/twinraid/(v[0-9.]+)/host-scripts/(\S+)", _section(16))
    assert {tag for tag, name in section_16 if name != "testdata.sh"} == {"v0.5.2"}
    assert {tag for tag, name in section_16 if name == "testdata.sh"} == {f"v{version[1]}"}
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _section_17_update())) == {
        f"v{version[1]}"
    }
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _rollback_0_5_2())) == {"v0.5.2"}
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _before_sync())) == {"v0.5.1"}
    assert set(re.findall(r"mantua1/twinraid/(v[0-9.]+)/", _rollback_section())) == {"v0.5.0"}


@pytest.mark.parametrize("name", sorted(RELEASE_0_5_1))
def test_documented_hashes_are_those_of_the_release(name: str) -> None:
    documented = set(re.findall(rf"([0-9a-f]{{64}})  /tmp/{re.escape(name)}", _before_sync()))
    assert documented == {RELEASE_0_5_1[name]}


@pytest.mark.parametrize("name", sorted(RELEASE_0_5_2))
def test_section_16_loads_the_scripts_of_0_5_2(name: str) -> None:
    documented = re.findall(rf"([0-9a-f]{{64}})  /tmp/{re.escape(name)}", _section(16))
    # testdata.sh kommt in 16.2 schon aus v0.5.3, mit report und bulk (F15).
    expected = _sha256(name) if name == "testdata.sh" else RELEASE_0_5_2[name]
    assert documented == [expected]


@pytest.mark.parametrize("name", ["source-guard.sh", "testdata.sh"])
def test_section_17_loads_the_scripts_of_this_version(name: str) -> None:
    """Herunterladen und Kopieren prüfen beide dieselbe SHA-256, die der Datei im Repo."""
    documented = re.findall(rf"([0-9a-f]{{64}})  /tmp/{re.escape(name)}", _section_17_update())
    assert documented == [_sha256(name)] * 2


def test_section_17_rolls_back_to_the_guard_of_0_5_2() -> None:
    documented = re.findall(r"([0-9a-f]{64})  /tmp/source-guard\.sh", _rollback_0_5_2())
    assert documented == [RELEASE_0_5_2["source-guard.sh"]] * 2
    assert "Rückweg beschrieben, bis 1.0 nicht auf den Servern geprüft." in _rollback_0_5_2()
    # Zuerst die Zeile entfernen und prüfen, dann Version 3 holen (Konzept 3.6, Protokoll 3).
    rollback = _rollback_0_5_2()
    remove = rollback.index("sed -i '/^min_snapshot_interval_minutes /d'")
    assert remove < rollback.index("check-config") < rollback.index("wget -O /tmp/source-guard.sh")


def test_section_17_names_the_header_of_the_guard() -> None:
    header = (SCRIPTS / "source-guard.sh").read_text(encoding="utf-8").splitlines()[1]
    assert "Version 4" in header
    assert f"Die zweite Zeile lautet `{header}`" in _section(17)


def test_test_value_of_the_interval_matches_the_guard() -> None:
    """T1: der kleinste erlaubte Wert minus eins wird abgelehnt, der kleinste gilt (F17)."""
    guard = (SCRIPTS / "source-guard.sh").read_text(encoding="utf-8")
    match = re.search(
        r"min_snapshot_interval_minutes\)\n\s+int_in \"\$\{value\}\" (\d+) (\d+)", guard
    )
    assert match is not None
    low = int(match[1])
    section = _section(17)
    assert f"echo 'min_snapshot_interval_minutes {low - 1}' >>" in section
    assert (
        f"s/^min_snapshot_interval_minutes {low - 1}$/min_snapshot_interval_minutes {low}/"
        in section
    )
    assert "`Konfiguration ungültig: bad_value (Zeile N).`" in section
    assert f"Abstand zwischen Snapshots {low} min (Standard 30)." in section


def test_every_restart_of_the_guard_in_section_17_checks_run_and_job() -> None:
    """restart bräche eine Übertragung ab; der Kasten startet nur ohne offenen Lauf und Job."""
    restarts = [b for b in _blocks(_section(17)) if "source-guard.sh restart" in b]
    assert len(restarts) == 1
    assert "grep -q '\"run\":null'" in restarts[0]
    assert '"state":"(closed|done|failed)"' in restarts[0]
    assert len(re.findall(r"Kasten aus 17\.3, Punkt 2 \(Neustart mit Prüfung\)", _section(17))) >= 3


_CHANGES: tuple[str, ...] = (
    "restart",
    "sed -i",
    ">>",
    "rm ",
    "cp ",
    "zfs set",
    "zfs destroy",
    "wget -O",
    "reset-run-interval",
    "testdata.sh change",
    "testdata.sh bulk",
    "rsync ",
)


def test_blocks_in_section_17_that_change_something_check_the_server() -> None:
    """Ein Kasten im falschen Terminal ändert nichts (Klein und Groß ohne Hostnamen erkannt)."""
    checked = 0
    for block in _blocks(_section(17)):
        if not any(change in block for change in _CHANGES):
            continue
        first = block.splitlines()[0]
        if first.startswith("docker exec twinraid-dev "):
            continue  # Test 0 am PC
        if first.startswith("wget -O /tmp/testdata.sh") or (
            "testdata.sh" in first and "sha256sum -c --status" in first
        ):
            continue  # testdata.sh gehört auf beide Server
        assert first in (f"if {ON_KLEIN}; then", f"if {ON_GROSS}; then", f"if {ON_KLEIN} &&"), first
        checked += 1
    assert checked >= 15


def test_guard_is_unchanged_since_0_5_2() -> None:
    """v0.5.3 ändert guard.sh nicht (Konzept 3.5); Abschnitt 16 gilt dafür weiter."""
    assert _sha256("guard.sh") == RELEASE_0_5_2["guard.sh"]


@pytest.mark.parametrize("name", sorted(HEADERS_0_5_2))
def test_section_16_names_the_header_of_the_scripts_of_0_5_2(name: str) -> None:
    assert f"Die zweite Zeile lautet `{HEADERS_0_5_2[name]}`" in _section(16)


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
    plain = "bash /boot/config/twinraid/testdata.sh change\n"
    # Z5 (17.10) ändert die Testdaten im Klon auf Groß; der Kasten prüft vorher den Server.
    on_gross = (
        f"if {ON_GROSS}; then\n"
        "  bash /boot/config/twinraid/testdata.sh change\n"
        "else\n"
        f'  echo "Nichts geändert: {NOT_GROSS}"\n'
        "fi\n"
    )
    with_change = [block for block in _blocks() if "testdata.sh change" in block]
    assert with_change == [plain, plain, plain, on_gross]
    warnings = re.findall(
        r"> \*\*Diesen Kasten allein und nur einmal einfügen\.\*\*[^\n]*\n\n *```bash\n"
        r"(?: *if \[[^\n]*\n)? *bash /boot/config/twinraid/testdata\.sh change\n",
        SETUP,
    )
    assert len(warnings) == 4


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


def test_large_test_data_come_from_bulk() -> None:
    """16.11 und 16.13 legen Zufallsdaten mit bulk an, keine eigenen Dateien (F6, F15)."""
    section = _section(16)
    assert "cp -p /mnt/user/isos" not in section
    assert "du -ch" not in section
    bulk = [block for block in _blocks(section) if "testdata.sh bulk" in block]
    assert bulk == [
        "bash /boot/config/twinraid/testdata.sh bulk <GiB>\n",
        "bash /boot/config/twinraid/testdata.sh bulk <GiB> /mnt/mainpool/twinraidtest/kopie2\n",
    ]
    for block in bulk:
        before = section[: section.index(f"```bash\n{block}")]
        assert "> **Diesen Kasten allein und nur einmal einfügen.**" in before[-700:]
    assert f"1 bis {_constant('BULK_MAX_GIB')}" in section
    assert f"{_constant('BULK_FREE_PERCENT')} % von `mainpool`" in section
    assert "7 Tage auf Klein** und **30 Tage auf Groß**" in section
    # Die Rate von 2,5 GbE ist nur ein Beispiel, keine Eigenschaft der Server.
    for match in re.finditer(r"298 MiB/s", section):
        assert "Beispiel" in section[max(0, match.start() - 120) : match.start()]


def test_mirror_on_the_target_is_only_verified_in_a_snapshot() -> None:
    """Im eingehängten Spiegel kann eine alte ACL stehen (Konzept 3.7)."""
    for line in re.findall(r"testdata\.sh verify (\S+)", SETUP):
        if "twinraid-mirror" in line:
            assert "/.zfs/snapshot/" in line, line
    assert "auch im Spiegel auf Groß" not in SETUP


def test_backup_before_0_5_3_is_only_made_on_klein() -> None:
    start = SETUP.index("### 17.2 ")
    section = SETUP[start : SETUP.index("\n### ", start + 1)]
    block = (
        f"if {ON_KLEIN}; then\n"
        "  cd /mnt/user/appdata/twinraid/config\n"
        "  [ -e twinraid.yaml.vor-0.5.3 ] || cp -p twinraid.yaml twinraid.yaml.vor-0.5.3\n"
        "  ls -l twinraid.yaml*\n"
    )
    assert block in _blocks(section)[0]
    assert "> **Diesen Kasten allein einfügen.**" in section[: section.index(block)]


def test_zfs_destroy_and_rollback_in_the_guide_are_never_recursive() -> None:
    assert not re.search(r"zfs (destroy|rollback) (-\w*[rRf]|.* -[rRf])", SETUP)


@pytest.mark.parametrize("number", [16, 17])
def test_sections_with_sync_only_name_test_data(number: int) -> None:
    """Im Repo nur Testnamen (CLAUDE.md, „Namen der echten Server“)."""
    section = _section(number)
    datasets = set(re.findall(r"mainpool/([A-Za-z0-9._-]+)", section))
    assert datasets <= {"twinraidtest", "twinraid-mirror"}
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", section)


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
        # Bis Abschnitt 16 zeigt sha256sum den Wert zum Vergleich, ab 17 prüft sha256sum -c.
        shown = len(re.findall(rf"sha256sum /tmp/{escaped}\n", SETUP))
        checked = len(re.findall(rf"  /tmp/{escaped}\" \| sha256sum -c\n", SETUP))
        assert shown + checked == count
        # Kopiert wird ab 17 nur, wenn die SHA-256 unmittelbar vorher stimmt.
        assert len(re.findall(rf"  /tmp/{escaped}\" \| sha256sum -c --status; then\n", SETUP)) == (
            checked
        )
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
