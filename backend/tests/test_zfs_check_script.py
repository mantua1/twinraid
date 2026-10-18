"""host-scripts/zfs-check.sh: Machbarkeitstest Z1 (Konzept 3.7 und 5.6).

Das Skript braucht root und ZFS; mit Pools in Dateien prüft es docker/zfs-check-test.sh
in der CI. Hier stehen die Prüfungen ohne ZFS: feste Befehlszeilen wie im Konzept,
nie rekursives Löschen, und dass es ohne root nichts tut.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "host-scripts" / "zfs-check.sh"
CONCEPT = REPO_ROOT / "docs" / "KONZEPT.md"


def _array(name: str) -> list[str]:
    text = SCRIPT.read_text()
    match = re.search(rf"^readonly {name}=\(\n(.*?)^\)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, name
    return match.group(1).split()


def test_receive_options_match_the_concept() -> None:
    """Die Befehlszeile ist dieselbe wie in Konzept 3.7 (und später in guard.sh)."""
    concept = CONCEPT.read_text()
    match = re.search(
        r"```\nzfs receive (.*?)mainpool/twinraid-mirror/<Name>\n```", concept, re.DOTALL
    )
    assert match is not None
    expected = [token for token in match.group(1).split() if token != "\\"]
    mountpoint = expected.index("mountpoint=/mnt/mainpool/twinraid-mirror/<Name>")
    assert expected[mountpoint - 1] == "-o"
    del expected[mountpoint - 1 : mountpoint + 1]
    assert "-x" not in expected
    assert _array("RECEIVE_OPTIONS") == expected


def test_no_property_from_a_stream_is_allowed() -> None:
    """Positivliste: nach dem Empfang darf keine Eigenschaft die Quelle received haben."""
    text = SCRIPT.read_text()
    assert "readonly RECEIVED_ALLOWED=()\n" in text


def test_old_receive_line_only_for_the_finding() -> None:
    """Die frühere Zeile mit -x dient nur dem Befund, nie dem eigentlichen Empfang."""
    text = SCRIPT.read_text()
    start = text.index("\ncheck_x_finding() {")
    end = text.index("\n}\n", start)
    finding = text[start:end]
    total = text.count('"${X_RECEIVE_OPTIONS[@]}"')
    assert total == finding.count('"${X_RECEIVE_OPTIONS[@]}"') == 2


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}() {{")
    return text[start : text.index("\n}\n", start)]


def test_lock_after_properties_from_the_stream() -> None:
    """Eigenschaften aus dem Strom: aushängen, dann canmount=noauto, dann Ablehnung."""
    function = _function("verify_and_mount")
    lock = function.index('zfs set -- canmount=noauto "${target}"')
    assert function.index("zfs unmount --") < lock < function.index("return 2")
    assert function.index("return 2") < function.index("zfs mount --")


def test_no_samba_share_on_unraid_target() -> None:
    """Auf Groß (Unraid, Modus target) setzt der böse Strom kein sharesmb=on."""
    lines = [
        line.strip()
        for line in SCRIPT.read_text().splitlines()
        if "sharesmb=on" in line and not line.lstrip().startswith("#")
    ]
    assert lines == ['elif ! zfs set -- sharesmb=on "${evil}" >/dev/null 2>&1; then']
    function = _function("check_hostile_stream")
    guard = 'if [[ "${MODE}" == target && -e /etc/unraid-version ]]; then'
    assert function.index(guard) < function.index("sharesmb=on")


def test_reset_as_in_the_concept() -> None:
    """Das Zurücksetzen in der Prüfung folgt Konzept 3.7: einzeln löschen, Rollback ohne -r."""
    concept = CONCEPT.read_text()
    assert "zfs set canmount=on mainpool/twinraid-mirror/<Name>" in concept
    function = _function("check_reset")
    assert 'zfs rollback -- "${mirror}@basis"' in function
    assert 'zfs destroy -- "${mirror}@${snap}"' in function
    assert 'zfs set -- canmount=on "${mirror}"' in function
    assert function.index("zfs inherit") < function.index("canmount=on")
    # zfs inherit nur für gemeldete Eigenschaften und nie für eine feste.
    assert function.count("zfs inherit") == 1
    assert function.index("is_fixed_property") < function.index("zfs inherit")
    assert "< <(received_properties" in function


def test_clone_options_match_the_concept() -> None:
    concept = CONCEPT.read_text()
    match = re.search(r"mit ausdrücklich gesetzten Eigenschaften \(([^)]*)\)", concept)
    assert match is not None
    expected = re.findall(r"`([a-z]+=[a-z0-9]+)`", match.group(1))
    options = _array("CLONE_OPTIONS")
    assert [options[i + 1] for i, token in enumerate(options) if token == "-o"] == expected


def test_script_never_destroys_recursively() -> None:
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert "eval" not in text
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    for line in code:
        assert not re.search(r"\b(zfs|zpool) destroy\b.*\s-[a-zA-Z]*[rRf]", line), line
        assert "rm -rf" not in line, line


def test_receive_with_force_only_as_unprivileged_user() -> None:
    """Die einzige Stelle mit -F prüft receive:append als Benutzer ohne root."""
    lines = SCRIPT.read_text().splitlines()
    forced = [i for i, line in enumerate(lines) if "zfs receive" in line and " -F" in line]
    assert len(forced) == 1
    assert "setpriv --reuid" in lines[forced[0] - 1]
    assert '"${deleg}"' in lines[forced[0]]


def test_fixed_paths() -> None:
    text = SCRIPT.read_text()
    for line in (
        "readonly POOL=mainpool\n",
        "readonly MIRROR_ROOT=mainpool/twinraid-mirror\n",
        "readonly TARGET_ROOT=mainpool/twinraid-mirror/zfs-check\n",
        "readonly PROPERTY=twinraid:zfs-check\n",
        "readonly FILE_PREFIX=/tmp/twinraid-check-\n",
    ):
        assert line in text, line
    assert "-o cachefile=none" in text


@pytest.mark.parametrize("arguments", [["target"], ["source"], ["cleanup", "target"]])
def test_refuses_without_root(arguments: list[str], tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
        ["/bin/bash", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "muss als root laufen" in result.stderr


@pytest.mark.parametrize("arguments", [[], ["unknown"], ["target", "extra"], ["cleanup"]])
def test_usage(arguments: list[str]) -> None:
    result = subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
        ["/bin/bash", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
