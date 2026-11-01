"""host-scripts/testdata.sh: Testdaten und Prüfliste für die Test-Share (ab v0.5.1).

Läuft ohne root; Besitzer und die Datei ohne Rechte prüft der CI-Test mit echtem ZFS
(docker/source-guard-sync-test.sh). Der Umfang ist für die Tests kleiner.
"""

import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "host-scripts" / "testdata.sh"
SMALLER = {
    "SMALL_DIRS": 3,
    "SMALL_PER_DIR": 60,
    "LARGE_COUNT": 1,
    "LARGE_MIB": 2,
    "SPARSE_MIB": 4,
    "DEPTH": 8,
}


@pytest.fixture(scope="module")
def script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    text = SCRIPT.read_text(encoding="utf-8")
    for name, value in SMALLER.items():
        text, count = re.subn(
            rf"^readonly {name}=.*$", f"readonly {name}={value}", text, flags=re.M
        )
        assert count == 1
    path = tmp_path_factory.mktemp("script") / "testdata.sh"
    path.write_text(text, encoding="utf-8")
    return path


def run(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - nur das Testskript aus dem Repo
        ["/bin/bash", str(script), *arguments], capture_output=True, text=True, check=False
    )


@pytest.fixture
def data(script: Path, tmp_path: Path) -> Iterator[Path]:
    folder = tmp_path / "testdata"
    result = run(script, "create", str(folder))
    assert result.returncode == 0, result.stderr
    yield folder
    # Ordner ohne Rechte oder mit Sticky-Bit stören das Aufräumen von pytest nicht: alles 0700.
    for root, directories, _files in os.walk(folder):
        for name in directories:
            path = Path(root) / name
            if not path.is_symlink():
                path.chmod(0o700)


def verify(script: Path, folder: Path) -> subprocess.CompletedProcess[str]:
    return run(script, "verify", str(folder))


def test_create_then_verify(script: Path, data: Path) -> None:
    result = verify(script, data)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Keine Abweichung (Lauf 0)." in result.stdout
    assert "Prüfliste: PRUEFLISTE.txt, SHA-256 " in result.stdout


def test_special_cases_exist(data: Path) -> None:
    names = {path.name for path in (data / "namen").iterdir()}
    assert {
        "mit leerzeichen.txt",
        "--",
        "neue\nzeile.txt",
        "umlaute äöüßÄÖÜ.txt",
        "n" * 255,
    } <= names
    assert (data / "gross" / "zufall-1.bin").stat().st_size == 2 * 1024 * 1024
    sparse = (data / "gross" / "loch.bin").stat()
    assert sparse.st_size == 4 * 1024 * 1024
    assert sparse.st_blocks * 512 < sparse.st_size
    assert stat.S_ISFIFO((data / "besonders" / "pipe").lstat().st_mode)
    assert (data / "links" / "relativ").readlink() == Path("ziel.txt")
    assert (data / "links" / "kaputt").readlink() == Path("fehlt.txt")
    assert (data / "hardlinks" / "a.txt").stat().st_ino == (
        data / "hardlinks" / "unterordner" / "c.txt"
    ).stat().st_ino
    assert stat.S_IMODE((data / "rechte" / "datei-4755").stat().st_mode) == 0o4755
    assert stat.S_IMODE((data / "rechte" / "ordner-1777").stat().st_mode) == 0o1777
    assert (data / "zeiten" / "1970.txt").stat().st_mtime == 0
    assert (data / "zeiten" / "2000-nanosekunden.txt").stat().st_mtime_ns == 946684800123456789
    assert os.lstat(data / "zeiten" / "link").st_mtime == 1000000000
    depth = list((data / "tief").rglob("ganz-unten.txt"))
    assert len(depth) == 1
    assert len(depth[0].relative_to(data).parts) == 10


def test_every_entry_is_in_the_list_once(data: Path) -> None:
    lines = [
        line
        for line in (data / "PRUEFLISTE.txt").read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    count = sum(len(directories) + len(files) for _root, directories, files in os.walk(data)) - len(
        ["PRUEFLISTE.txt", ".twinraid-testdata"]
    )
    assert len(lines) == count
    # Die Inode-Nummer ist auf jedem Server anders; die Liste hält nur Gruppen fest.
    groups = [line.split()[6] for line in lines if "/hardlinks/" in line and line[0] == "f"]
    assert sorted(groups) == ["1", "1", "1", "2", "2"]


def tamper_content(data: Path) -> None:
    with (data / "links" / "ziel.txt").open("r+b") as handle:
        handle.write(b"X")


def tamper_mode(data: Path) -> None:
    (data / "rechte" / "datei-0644").chmod(0o666)


def tamper_time(data: Path) -> None:
    os.utime(data / "zeiten" / "2100.txt", (0, 5))


def tamper_dir_time(data: Path) -> None:
    os.utime(data / "zeiten" / "ordner", (0, 5))


def tamper_link_time(data: Path) -> None:
    os.utime(data / "zeiten" / "link", (0, 5), follow_symlinks=False)


def tamper_delete(data: Path) -> None:
    (data / "namen" / "--").unlink()


def tamper_add(data: Path) -> None:
    (data / "besonders" / "dazu").write_text("x")


def tamper_rename(data: Path) -> None:
    (data / "hardlinks" / "d.txt").rename(data / "hardlinks" / "d2.txt")


def tamper_symlink(data: Path) -> None:
    (data / "links" / "relativ").unlink()
    (data / "links" / "relativ").symlink_to("anders.txt")


def tamper_hardlink(data: Path) -> None:
    path = data / "hardlinks" / "b.txt"
    content = path.read_bytes()
    path.unlink()
    path.write_bytes(content)
    os.utime(path, ns=(0, (data / "hardlinks" / "a.txt").stat().st_mtime_ns))


def tamper_fifo(data: Path) -> None:
    (data / "besonders" / "pipe").unlink()
    (data / "besonders" / "pipe").write_text("")


@pytest.mark.parametrize(
    "tamper",
    [
        tamper_content,
        tamper_mode,
        tamper_time,
        tamper_dir_time,
        tamper_link_time,
        tamper_delete,
        tamper_add,
        tamper_rename,
        tamper_symlink,
        tamper_hardlink,
        tamper_fifo,
    ],
)
def test_verify_finds_every_difference(script: Path, data: Path, tamper: object) -> None:
    assert callable(tamper)
    tamper(data)
    result = verify(script, data)
    assert result.returncode == 1
    assert "ABWEICHUNG" in result.stdout


def test_verify_finds_changed_xattrs_and_acls(script: Path, data: Path) -> None:
    list_text = (data / "PRUEFLISTE.txt").read_text(encoding="utf-8")
    if "# xattr yes" in list_text:
        os.setxattr(data / "xattr" / "datei.txt", "user.twinraid.text", b"anders")
        assert verify(script, data).returncode == 1
        os.setxattr(data / "xattr" / "datei.txt", "user.twinraid.text", "Text äöü".encode())
        assert verify(script, data).returncode == 0
    if "# acl yes" in list_text:
        subprocess.run(  # noqa: S603
            ["/usr/bin/setfacl", "-m", "u:1000:---", str(data / "acl" / "datei.txt")], check=True
        )
        assert verify(script, data).returncode == 1


def test_verify_changes_nothing(script: Path, data: Path) -> None:
    def state() -> dict[str, tuple[int, int]]:
        return {
            str(path): (path.lstat().st_mtime_ns, path.lstat().st_mode)
            for path in [data, *data.rglob("*")]
        }

    before = state()
    assert verify(script, data).returncode == 0
    assert state() == before


def test_change_then_verify(script: Path, data: Path) -> None:
    result = run(script, "change", str(data))
    assert result.returncode == 0, result.stderr
    assert "50 gelöscht, 20 geändert, 30 neu, 10 umbenannt" in result.stdout
    assert len(list((data / "viele" / "ordner-002").glob("umbenannt-1-*"))) == 10
    assert len(list((data / "neu").iterdir())) == 30
    assert stat.S_IMODE((data / "rechte" / "datei-0664").stat().st_mode) == 0o660
    assert "Keine Abweichung (Lauf 1)." in verify(script, data).stdout
    assert run(script, "change", str(data)).returncode == 0
    assert "Keine Abweichung (Lauf 2)." in verify(script, data).stdout


def test_change_stops_when_too_few_files_are_left(script: Path, data: Path) -> None:
    results = [run(script, "change", str(data)) for _ in range(2)]
    assert [result.returncode for result in results] == [0, 0]
    assert run(script, "change", str(data)).returncode == 2


def test_create_refuses_an_existing_folder(script: Path, data: Path) -> None:
    result = run(script, "create", str(data))
    assert result.returncode == 2
    assert "gibt es schon" in result.stderr


@pytest.mark.parametrize("command", ["create", "change"])
def test_writes_only_into_a_folder_named_testdata(
    script: Path, tmp_path: Path, command: str
) -> None:
    other = tmp_path / "wichtig"
    other.mkdir()
    (other / "datei").write_text("bleibt")
    result = run(script, command, str(other))
    assert result.returncode == 2
    assert "testdata" in result.stderr
    assert [path.name for path in other.iterdir()] == ["datei"]


def test_change_needs_the_marker(script: Path, tmp_path: Path) -> None:
    folder = tmp_path / "testdata"
    folder.mkdir()
    (folder / "datei").write_text("bleibt")
    result = run(script, "change", str(folder))
    assert result.returncode == 2
    assert "keine Testdaten" in result.stderr
    assert [path.name for path in folder.iterdir()] == ["datei"]


@pytest.mark.parametrize(
    "path", ["relativ/testdata", "/mnt/../etc/testdata", "/mnt/a b/testdata", "/"]
)
def test_refuses_odd_paths(script: Path, path: str) -> None:
    assert run(script, "verify", path).returncode == 2


def test_verify_without_a_list(script: Path, tmp_path: Path) -> None:
    result = run(script, "verify", str(tmp_path))
    assert result.returncode == 2
    assert "keine Prüfliste" in result.stderr


@pytest.mark.parametrize("arguments", [[], ["unknown"], ["verify", "/mnt", "zu-viel"]])
def test_usage(script: Path, arguments: list[str]) -> None:
    result = run(script, *arguments)
    assert result.returncode == 2
    assert "Aufruf:" in result.stderr


def test_default_folder_is_the_test_share() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "readonly DEFAULT_DIR=/mnt/mainpool/twinraidtest/testdata\n" in text


def test_no_eval_and_fixed_path() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"\beval\b", text)
    assert "export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n" in text
    assert "export LC_ALL=C\n" in text
