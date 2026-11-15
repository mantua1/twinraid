"""host-scripts/testdata.sh report und bulk (ab v0.5.3, Konzept 5.6).

Mit nachgebautem zfs, findmnt, guard.sh und Syslog. Jeder Punkt von report einmal in Ordnung
und einmal abweichend; die Ausgabe enthält nie Zeilen aus dem Syslog. Den Lauf mit echtem ZFS
prüft docker/source-guard-sync-test.sh.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT
from tests.test_testdata_script import SMALLER

SCRIPT = REPO_ROOT / "host-scripts" / "testdata.sh"
GUARD = REPO_ROOT / "host-scripts" / "guard.sh"
NAME = "twinraidtest"
SOURCE_DS = f"mainpool/{NAME}"
MIRROR_DS = f"mainpool/twinraid-mirror/{NAME}"
HELD = "twinraid-20261105T000000Z"
NEWEST = "twinraid-20261106T000000Z"
# Stehen im Syslog und dürfen nie in der Ausgabe auftauchen.
SECRET_UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SECRET_CONTAINER = "geheimer-container"
DAY = 86400

FAKE_ZFS = """#!{python}
import json, sys
state = json.load(open({state!r}))
argv = sys.argv[1:]
src = state["source"]
mir = state["mirror"]

def out(rows):
    for row in rows:
        print("\\t".join(str(v) for v in row))
    sys.exit(0)

if argv == ["get", "-H", "-p", "-d", "1", "-t", "snapshot", "-o", "name,property,value,source",
            "--", "twinraid:created-by,creation,userrefs,createtxg", src["dataset"]]:
    if src.get("unreadable"):
        sys.exit(1)
    rows = []
    for s in src["snapshots"]:
        name = src["dataset"] + "@" + s["name"]
        rows.append([name, "twinraid:created-by", "source-guard" if s["own"] else "-",
                     "local" if s["own"] else "-"])
        rows.append([name, "creation", s["creation"], "-"])
        rows.append([name, "userrefs", s["userrefs"], "-"])
        rows.append([name, "createtxg", s["txg"], "-"])
    out(rows)
if argv[:3] == ["holds", "-H", "--"]:
    name = argv[3].split("@", 1)[1]
    out([[argv[3], tag, "Fri Nov  6 00:00 2026"] for tag in src["holds"].get(name, [])])
if argv == ["list", "-H", "-p", "-d", "1", "-t", "bookmark", "-o", "name,createtxg", "-s",
            "createtxg", "--", src["dataset"]]:
    out([[src["dataset"] + "#" + b, i] for i, b in enumerate(src["bookmarks"])])
if argv[:6] == ["get", "-H", "-o", "property,value,source", "--"] or (
        argv[:5] == ["get", "-H", "-o", "property,value,source", "--"]):
    wanted = argv[5].split(",")
    if argv[6] != mir["dataset"]:
        sys.exit(1)
    out([[p, *mir["props"][p]] for p in wanted if p in mir["props"]])
if argv == ["get", "-H", "-r", "-t", "filesystem,volume,snapshot", "-o", "name,property", "-s",
            "received", "--", "all", mir["dataset"]]:
    out([[mir["dataset"], p] for p in mir["received"]])
if argv == ["get", "-H", "-p", "-d", "1", "-t", "snapshot", "-o", "name,value,source", "--",
            "twinraid:received", mir["dataset"]]:
    out([[mir["dataset"] + "@" + s["name"], s["received"], s["source"]] for s in mir["snapshots"]])
if argv == ["list", "-H", "-t", "snapshot", "-d", "1", "-o", "name", "-s", "createtxg", "--",
            mir["dataset"]]:
    out([[mir["dataset"] + "@" + s["name"]] for s in mir["snapshots"]])
if argv[:4] == ["get", "-H", "-o", "value"] and argv[5:] == ["--", mir["dataset"]]:
    print({{"mountpoint": mir["mountpoint"], "mounted": "yes"}}[argv[4]])
    sys.exit(0)
if argv == ["list", "-H", "-p", "-o", "used,available", "--", "mainpool"]:
    print(f"{{state['pool_used']}}\\t{{state['pool_available']}}")
    sys.exit(0)
sys.exit(99)
"""

FAKE_FINDMNT = """#!{python}
import json, sys
state = json.load(open({state!r}))
if sys.argv[1:-1] == ["-n", "-r", "-o", "TARGET,SOURCE,FSTYPE", "-M"] and state["findmnt"]:
    print(state["findmnt"])
    sys.exit(0)
sys.exit(1)
"""

FAKE_GUARD = """#!/bin/bash
[[ "${{SSH_ORIGINAL_COMMAND:-}}" == check && "$1" == sync ]] || exit 2
cat {answer}
exit "$(cat {code})"
"""


def fixed_props_of_guard() -> dict[str, str]:
    """Die festen Werte aus RECEIVE_OPTIONS in guard.sh."""
    text = GUARD.read_text(encoding="utf-8")
    block = re.search(r"^readonly RECEIVE_OPTIONS=\((.*?)^\)", text, flags=re.M | re.S)
    assert block is not None
    pairs = re.findall(r"-o (\S+)", block.group(1))
    return dict(pair.split("=", 1) for pair in pairs)


def fixed_props_of_testdata() -> dict[str, str]:
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"^readonly FIXED_PROPS=\((.*?)^\)", text, flags=re.M | re.S)
    assert block is not None
    return dict(pair.split("=", 1) for pair in block.group(1).split())


def test_fixed_props_match_guard() -> None:
    """Die Liste in testdata.sh läuft nicht auseinander mit der Empfangszeile von guard.sh."""
    assert fixed_props_of_testdata() == fixed_props_of_guard()


def syslog_line(tag: str, message: str) -> str:
    return f"Nov  8 10:00:00 Server {tag}: {message}"


@dataclass
class Host:
    """Nachgebauter Server; state wird vor jedem Lauf geschrieben."""

    tmp: Path
    state: dict[str, Any] = field(default_factory=dict)
    syslog: list[str] = field(default_factory=list)
    conf: str | None = "snapshot_keep_days 7   # Tage\n"
    check: str = ""
    check_code: int = 0

    def __post_init__(self) -> None:
        now = int(time.time())
        self.mountpoint = self.tmp / "mirror" / NAME
        self.mountpoint.mkdir(parents=True)
        self.bulk_root = self.tmp / "twinraidtest"
        self.bulk_root.mkdir()
        self.state = {
            "source": {
                "dataset": SOURCE_DS,
                "snapshots": [
                    {
                        "name": "manuell",
                        "own": False,
                        "creation": now - 90 * DAY,
                        "userrefs": 0,
                        "txg": 5,
                    },
                    {
                        "name": "twinraid-20261101T000000Z",
                        "own": True,
                        "creation": now - 3 * DAY,
                        "userrefs": 0,
                        "txg": 10,
                    },
                    # Die Basis darf älter sein als die Aufbewahrung.
                    {
                        "name": HELD,
                        "own": True,
                        "creation": now - 20 * DAY,
                        "userrefs": 1,
                        "txg": 20,
                    },
                    {"name": NEWEST, "own": True, "creation": now - 3600, "userrefs": 0, "txg": 30},
                ],
                "holds": {HELD: ["twinraid-base"]},
                "bookmarks": ["twinraid-20261101T000000Z", HELD],
            },
            "mirror": {
                "dataset": MIRROR_DS,
                "mountpoint": str(self.mountpoint),
                "props": {
                    **{p: [v, "local"] for p, v in fixed_props_of_testdata().items()},
                    "mountpoint": [f"{self.tmp}/mirror/{NAME}", "local"],
                },
                "received": [],
                "snapshots": [
                    {
                        "name": "twinraid-20261101T000000Z",
                        "received": now - 3 * DAY,
                        "source": "local",
                    },
                    {"name": HELD, "received": now - DAY, "source": "local"},
                ],
            },
            "findmnt": f"{self.tmp}/mirror/{NAME} {MIRROR_DS} zfs",
            "pool_used": 10 * 1024**2,
            "pool_available": 90 * 1024**2,
        }
        self.check = (
            "twinraid-guard 3 sync ok\n"
            f"mirror {NAME} state=mirror props=ok count=2 newest_guid=1 newest_created=1"
            " newest_received=1 oldest_created=1\n"
            "summary root=ok conf=ok deleg=ok receive=idle clones=0 available=1 size=2"
            " reserve_percent=15 keep_days=30 min_interval_minutes=10\n"
        )
        self.syslog = [
            syslog_line(
                "twinraid-source-guard", f"sending {NAME} failed (send_failed unreachable)"
            ),
            syslog_line("twinraid-source-guard", "started (version 4)"),
            syslog_line("twinraid-source-guard", f"order 3: stop {SECRET_CONTAINER}: stopped"),
            syslog_line("twinraid-source-guard", f"sent {NAME}"),
            syslog_line("twinraid-guard", f"vm-start of VM {SECRET_UUID} requested by TwinRaid"),
            syslog_line("twinraid-guard", f"receive into mirror {NAME} requested"),
            syslog_line("twinraid-guard", f"receive into mirror {NAME} started"),
            syslog_line("twinraid-guard", f"receive into mirror {NAME} finished"),
            syslog_line("twinraid-guard", f"refused: vm-running {SECRET_UUID}"),
        ]
        self.script = self.build()

    def create_test_data(self) -> None:
        """Testdaten im Snapshot, den der Spiegel als neuesten meldet."""
        newest = self.mountpoint / ".zfs" / "snapshot" / HELD / "testdata"
        newest.parent.mkdir(parents=True)
        assert self.run("create", str(newest)).returncode == 0

    def build(self) -> Path:
        state = self.tmp / "state.json"
        tools = {
            "zfs": FAKE_ZFS.format(python=sys.executable, state=str(state)),
            "findmnt": FAKE_FINDMNT.format(python=sys.executable, state=str(state)),
            "guard.sh": FAKE_GUARD.format(
                answer=self.tmp / "check.txt", code=self.tmp / "check.code"
            ),
        }
        for name, text in tools.items():
            (self.tmp / name).write_text(text)
            (self.tmp / name).chmod(0o755)
        text = SCRIPT.read_text(encoding="utf-8")
        replacements = {
            **SMALLER,
            "ZFS": str(self.tmp / "zfs"),
            "FINDMNT": str(self.tmp / "findmnt"),
            "GUARD_SH": str(self.tmp / "guard.sh"),
            "SYSLOG": str(self.tmp / "syslog"),
            "SOURCE_GUARD_CONF": str(self.tmp / "source-guard.conf"),
            "MIRROR_MOUNT": f"{self.tmp}/mirror",
            "MIRROR_ROOTS": f"({self.tmp}/mirror /mnt/user/twinraid-mirror)",
            "BULK_ROOT": str(self.bulk_root),
            "GIB": 1024,
            "RANDOM_SOURCE": str(self.tmp / "random"),
        }
        for name, value in replacements.items():
            text, count = re.subn(
                rf"^readonly {name}=.*$", f"readonly {name}={value}", text, flags=re.M
            )
            assert count == 1, name
        (self.tmp / "random").write_bytes(os.urandom(4096))
        script = self.tmp / "testdata.sh"
        script.write_text(text, encoding="utf-8")
        return script

    def write(self) -> None:
        (self.tmp / "state.json").write_text(json.dumps(self.state))
        (self.tmp / "syslog").write_text("".join(f"{line}\n" for line in self.syslog))
        conf = self.tmp / "source-guard.conf"
        if self.conf is None:
            conf.unlink(missing_ok=True)
        else:
            conf.write_text(self.conf)
        (self.tmp / "check.txt").write_text(self.check)
        (self.tmp / "check.code").write_text(str(self.check_code))

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.write()
        return subprocess.run(  # noqa: S603 - nur das Testskript aus dem Repo
            ["/bin/bash", str(self.script), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )

    def report(self, side: str) -> subprocess.CompletedProcess[str]:
        result = self.run("report", side)
        assert SECRET_UUID not in result.stdout + result.stderr
        assert SECRET_CONTAINER not in result.stdout + result.stderr
        assert "Nov  8" not in result.stdout + result.stderr
        return result


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


@pytest.fixture
def target(tmp_path: Path) -> Host:
    host = Host(tmp_path)
    host.create_test_data()
    return host


def line_of(result: subprocess.CompletedProcess[str], point: str) -> str:
    lines = [line for line in result.stdout.splitlines() if re.match(rf"^\S+ +{point}: ", line)]
    assert len(lines) == 1, result.stdout
    return lines[0]


def assert_only_deviation(result: subprocess.CompletedProcess[str], point: str) -> str:
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("1 Abweichung."), result.stdout
    line = line_of(result, point)
    assert line.startswith("ABWEICHUNG"), result.stdout
    return line


# --- report source ---------------------------------------------------------------------


def test_report_source_all_fine(host: Host) -> None:
    result = host.report("source")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith(f"testdata.sh report source: {SOURCE_DS}\n")
    assert line_of(result, "Hold") == f"OK         Hold: twinraid-base an {HELD}"
    assert line_of(result, "Lesezeichen").endswith(f"#{HELD} ist das neueste von 2 eigenen")
    snapshots = line_of(result, "Snapshots")
    assert snapshots.startswith("OK")
    assert "3 eigene (höchstens 50), der älteste vor 20 Tagen" in snapshots
    assert "Aufbewahrung 7 Tage (source-guard.conf); 1 fremde" in snapshots
    # Der Fehler vor dem letzten Start des Wächters zählt nicht.
    assert line_of(result, "Syslog").startswith("OK         Syslog: seit dem letzten Start")
    assert f"muss {HELD} heißen" in result.stdout
    assert result.stdout.endswith("Alles in Ordnung.\n")


def test_report_source_without_hold(host: Host) -> None:
    host.state["source"]["holds"] = {}
    result = host.report("source")
    assert result.returncode == 1
    assert line_of(result, "Hold").startswith("ABWEICHUNG")
    assert line_of(result, "Lesezeichen").startswith("INFO")
    assert "Vergleich mit Groß" not in result.stdout


def test_report_source_two_holds(host: Host) -> None:
    host.state["source"]["snapshots"][3]["userrefs"] = 1
    host.state["source"]["holds"][NEWEST] = ["twinraid-base"]
    line = line_of(host.report("source"), "Hold")
    assert "2 eigene Snapshots haben den Hold" in line


def test_report_source_ignores_other_holds(host: Host) -> None:
    host.state["source"]["snapshots"][3]["userrefs"] = 1
    host.state["source"]["holds"][NEWEST] = ["fremd"]
    assert host.report("source").returncode == 0


@pytest.mark.parametrize(
    ("bookmarks", "message"),
    [
        ([], "kein eigenes Lesezeichen"),
        ([HELD, NEWEST], f"#{HELD} gibt es, das neueste ist aber #{NEWEST}"),
        (["twinraid-20261101T000000Z"], f"kein Lesezeichen #{HELD}"),
    ],
)
def test_report_source_bookmarks(host: Host, bookmarks: list[str], message: str) -> None:
    host.state["source"]["bookmarks"] = bookmarks
    assert message in assert_only_deviation(host.report("source"), "Lesezeichen")


def test_report_source_old_snapshot(host: Host) -> None:
    # Acht Tage alt bei sieben Tagen Aufbewahrung: noch im Spielraum von einem Tag.
    host.state["source"]["snapshots"][1]["creation"] = int(time.time()) - 8 * DAY + 600
    assert host.report("source").returncode == 0
    host.state["source"]["snapshots"][1]["creation"] = int(time.time()) - 8 * DAY - 600
    line = assert_only_deviation(host.report("source"), "Snapshots")
    assert "1 außer der Basis älter als 7 Tage" in line


def test_report_source_keep_days_from_conf(host: Host) -> None:
    host.state["source"]["snapshots"][1]["creation"] = int(time.time()) - 9 * DAY
    host.conf = "allow x\nsnapshot_keep_days 10\n"
    line = line_of(host.report("source"), "Snapshots")
    assert line.startswith("OK")
    assert "Aufbewahrung 10 Tage (source-guard.conf)" in line
    host.conf = None
    line = line_of(host.report("source"), "Snapshots")
    assert line.startswith("ABWEICHUNG")
    assert "Aufbewahrung 7 Tage (Standard)" in line
    # Nur das feste Muster zählt; der Wert wird nie ausgewertet.
    marker = host.tmp / "ausgefuehrt"
    host.conf = f"snapshot_keep_days $(touch {marker})\n"
    assert "(Standard)" in line_of(host.report("source"), "Snapshots")
    assert not marker.exists()


def test_report_source_too_many_snapshots(host: Host) -> None:
    now = int(time.time())
    host.state["source"]["snapshots"] += [
        {
            "name": f"twinraid-202611{i:02d}T120000Z",
            "own": True,
            "creation": now - 60,
            "userrefs": 0,
            "txg": 100 + i,
        }
        for i in range(1, 49)
    ]
    assert "mehr als 50" in assert_only_deviation(host.report("source"), "Snapshots")


def test_report_source_counts_odd_names_as_foreign(host: Host) -> None:
    """Namen aus zfs werden nie ausgewertet, auch nicht als Index eines Arrays."""
    marker = host.tmp / "ausgefuehrt"
    for name in (f"twinraid-$(touch {marker})", f"twinraid-`touch {marker}`"):
        host.state["source"]["snapshots"].append(
            {"name": name, "own": True, "creation": 1, "userrefs": 1, "txg": 40}
        )
        host.state["source"]["holds"][name] = ["twinraid-base"]
    result = host.report("source")
    assert result.returncode == 0, result.stdout
    assert "; 3 fremde" in line_of(result, "Snapshots")
    assert "touch" not in result.stdout
    assert not marker.exists()


def test_report_source_no_own_snapshots(host: Host) -> None:
    for snapshot in host.state["source"]["snapshots"]:
        snapshot["own"] = False
    result = host.report("source")
    assert "keine eigenen Snapshots; 4 fremde" in line_of(result, "Snapshots")


@pytest.mark.parametrize(
    "message",
    [
        f"sending {NAME} failed (send_failed unreachable)",
        f"dataset {NAME}: hold on the new base failed, the old hold stays",
        f"dataset {NAME}: bookmark failed",
        "retention: 2 own snapshots destroyed, 1 failed",
    ],
)
def test_report_source_syslog_failures(host: Host, message: str) -> None:
    host.syslog.append(syslog_line("twinraid-source-guard", message))
    line = assert_only_deviation(host.report("source"), "Syslog")
    assert "1 Fehlerzeilen" in line
    assert message not in host.report("source").stdout


def test_report_source_syslog_ignores_other_datasets(host: Host) -> None:
    host.syslog += [
        syslog_line("twinraid-source-guard", f"sending {NAME}2 failed (send_error)"),
        syslog_line("twinraid-source-guard", "retention: 2 own snapshots destroyed, 0 failed"),
    ]
    assert host.report("source").returncode == 0


def test_report_source_syslog_without_start(host: Host) -> None:
    host.syslog = [line for line in host.syslog if "started (version" not in line]
    line = assert_only_deviation(host.report("source"), "Syslog")
    assert "im ganzen Syslog" in line


def test_report_source_unreadable(host: Host) -> None:
    host.state["source"]["unreadable"] = True
    result = host.report("source")
    assert result.returncode == 1
    assert "ist nicht lesbar" in line_of(result, "Snapshots")


# --- report target ---------------------------------------------------------------------


def test_report_target_all_fine(target: Host) -> None:
    result = target.report("target")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith(f"testdata.sh report target: {MIRROR_DS}\n")
    assert line_of(result, "check").startswith(
        "OK         check: guard.sh 3 sync ok; state=mirror props=ok count=2;"
    )
    assert line_of(result, "Feste Werte").startswith("OK         Feste Werte: alle 25 mit")
    assert line_of(result, "received").startswith("OK")
    assert line_of(result, "Einhängen").startswith("OK")
    assert line_of(result, "twinraid:received").endswith(
        f"an allen 2 Snapshots mit Quelle local; neuester {HELD}"
    )
    assert line_of(result, "Testdaten").endswith(
        f"verify-latest ohne Abweichung, Snapshot {HELD}, Lauf 0"
    )
    assert line_of(result, "Syslog").startswith("OK")
    assert f"Der gehaltene Snapshot auf Klein muss {HELD} heißen" in result.stdout
    assert result.stdout.endswith("Alles in Ordnung.\n")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("state=mirror props=ok", "state=mirror props=mismatch"),
        ("state=mirror", "state=absent"),
        ("deleg=ok", "deleg=mismatch"),
        ("receive=idle", "receive=busy"),
        ("clones=0", "clones=1"),
        ("conf=ok", "conf=invalid"),
        ("twinraid-guard 3", "twinraid-guard 2"),
    ],
)
def test_report_target_check(target: Host, old: str, new: str) -> None:
    target.check = target.check.replace(old, new)
    line = assert_only_deviation(target.report("target"), "check")
    assert new.split()[-1] in line or "guard.sh 2" in line


def test_report_target_check_fails(target: Host) -> None:
    target.check_code = 255
    assert "Exit-Code 255" in assert_only_deviation(target.report("target"), "check")
    target.check_code = 0
    target.check = target.check.replace(f"mirror {NAME} ", "mirror anders ")
    assert "steht nicht in der Antwort" in assert_only_deviation(target.report("target"), "check")
    target.check = "twinraid-guard 3 sync ok\n"
    assert "unvollständig" in assert_only_deviation(target.report("target"), "check")


@pytest.mark.parametrize(
    ("prop", "value", "source", "shown"),
    [
        ("readonly", "off", "local", "readonly=off (Quelle local)"),
        ("exec", "off", "inherited from mainpool", "exec=off (Quelle ?)"),
        ("canmount", "noauto", "local", "canmount=noauto"),
        ("mountpoint", "/anderswo", "local", "mountpoint=/anderswo"),
        ("twinraid:mirror", "1", "received", "twinraid:mirror=1 (Quelle received)"),
        ("quota", "$(id)", "local", "quota=? (Quelle local)"),
    ],
)
def test_report_target_fixed_values(
    target: Host, prop: str, value: str, source: str, shown: str
) -> None:
    target.state["mirror"]["props"][prop] = [value, source]
    assert shown in assert_only_deviation(target.report("target"), "Feste Werte")


def test_report_target_compression_on_counts_as_lz4(target: Host) -> None:
    target.state["mirror"]["props"]["compression"] = ["on", "local"]
    assert target.report("target").returncode == 0


def test_report_target_missing_fixed_value(target: Host) -> None:
    del target.state["mirror"]["props"]["sharenfs"]
    assert "1 Werte fehlen" in assert_only_deviation(target.report("target"), "Feste Werte")


def test_report_target_received(target: Host) -> None:
    target.state["mirror"]["received"] = ["quota", "mountpoint"]
    line = assert_only_deviation(target.report("target"), "received")
    assert "2 Eigenschaften mit Quelle received" in line


@pytest.mark.parametrize(
    ("findmnt", "message"),
    [("", "nicht eingehängt"), ("{mount} mainpool/anders zfs", "anders eingehängt")],
)
def test_report_target_mount(target: Host, findmnt: str, message: str) -> None:
    target.state["findmnt"] = findmnt.format(mount=f"{target.tmp}/mirror/{NAME}")
    assert message in assert_only_deviation(target.report("target"), "Einhängen")


def test_report_target_twinraid_received(target: Host) -> None:
    target.state["mirror"]["snapshots"][0]["source"] = "received"
    line = assert_only_deviation(target.report("target"), "twinraid:received")
    assert "fehlt an 1 von 2 Snapshots" in line


def test_report_target_test_data(target: Host) -> None:
    data = target.mountpoint / ".zfs" / "snapshot" / HELD / "testdata"
    (data / "gross" / "zufall-1.bin").write_bytes(b"anders")
    line = assert_only_deviation(target.report("target"), "Testdaten")
    assert f"testdata.sh verify-latest {MIRROR_DS}" in line


def test_report_target_without_test_data(target: Host) -> None:
    target.state["mirror"]["snapshots"].append(
        {"name": NEWEST, "received": int(time.time()), "source": "local"}
    )
    (target.mountpoint / ".zfs" / "snapshot" / NEWEST).mkdir()
    result = target.report("target")
    assert result.returncode == 0, result.stdout
    assert line_of(result, "Testdaten").startswith("INFO")


@pytest.mark.parametrize(
    ("messages", "text"),
    [
        (
            [f"receive into mirror {NAME} requested", "refused: props-mismatch"],
            "endete nicht mit finished (abgelehnt: props-mismatch)",
        ),
        (
            [
                f"receive into mirror {NAME} requested",
                f"receive into mirror {NAME} started",
                f"failed: receive into mirror {NAME} (zfs exit code 1)",
            ],
            "endete nicht mit finished",
        ),
        ([f"mirror {NAME} locked"], "1 Warnungen"),
        ([f"mirror {NAME}: twinraid:received not set"], "1 Warnungen"),
        ([f"retention of mirror {NAME}: 1 snapshots destroyed, 2 failed"], "1 Warnungen"),
    ],
)
def test_report_target_syslog(target: Host, messages: list[str], text: str) -> None:
    target.syslog += [syslog_line("twinraid-guard", message) for message in messages]
    line = assert_only_deviation(target.report("target"), "Syslog")
    assert text in line
    for message in messages:
        assert message not in line


def test_report_target_syslog_ignores_other_mirrors(target: Host) -> None:
    target.syslog += [
        syslog_line("twinraid-guard", f"receive into mirror {NAME}2 requested"),
        syslog_line("twinraid-guard", "refused: too-soon"),
        syslog_line("twinraid-guard", f"mirror {NAME}2 locked"),
        syslog_line(
            "twinraid-guard", f"retention of mirror {NAME}: 1 snapshots destroyed, 0 failed"
        ),
    ]
    assert target.report("target").returncode == 0


def test_report_target_syslog_without_receive(target: Host) -> None:
    target.syslog = [line for line in target.syslog if "receive into" not in line]
    assert "kein Empfang" in assert_only_deviation(target.report("target"), "Syslog")


def test_report_target_unreadable_syslog(target: Host) -> None:
    target.write()
    (target.tmp / "syslog").chmod(0)
    if os.access(target.tmp / "syslog", os.R_OK):
        pytest.skip("läuft als root")
    result = subprocess.run(  # noqa: S603 - nur das Testskript aus dem Repo
        ["/bin/bash", str(target.script), "report", "target"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "ist nicht lesbar" in line_of(result, "Syslog")


@pytest.mark.parametrize(
    "arguments",
    [
        ["report"],
        ["report", "beide"],
        ["report", "source", NAME, "zu-viel"],
    ],
)
def test_report_usage(host: Host, arguments: list[str]) -> None:
    result = host.run(*arguments)
    assert result.returncode == 2
    assert "report source|target [Name]" in result.stderr


@pytest.mark.parametrize("name", ["../etc", "-x", "a/b", "mit leer", "x" * 65])
def test_report_checks_the_name(host: Host, name: str) -> None:
    result = host.run("report", "target", name)
    assert result.returncode == 2
    assert "Ungültiger Name" in result.stderr


def test_report_changes_nothing() -> None:
    """report ruft zfs nur lesend auf: get, list und holds."""
    text = SCRIPT.read_text(encoding="utf-8")
    body = text[text.index("# --- report (ab v0.5.3)") : text.index("# --- bulk (ab v0.5.3)")]
    calls = re.findall(r'"\$\{ZFS\}" +(\S+)', body)
    assert calls
    assert set(calls) <= {"get", "list", "holds"}


def test_report_defaults() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for line in (
        "readonly DEFAULT_NAME=twinraidtest\n",
        "readonly GUARD_SH=/boot/config/twinraid/guard.sh\n",
        "readonly SOURCE_GUARD_CONF=/boot/config/twinraid/source-guard.conf\n",
        "readonly SYSLOG=/var/log/syslog\n",
        "readonly MIRROR_MOUNT=/mnt/mainpool/twinraid-mirror\n",
        "readonly BULK_ROOT=/mnt/mainpool/twinraidtest\n",
        "readonly GIB=1073741824\n",
        "readonly RANDOM_SOURCE=/dev/urandom\n",
    ):
        assert line in text


# --- bulk ------------------------------------------------------------------------------


def test_bulk_writes_random_files(host: Host) -> None:
    result = host.run("bulk", "3")
    assert result.returncode == 0, result.stderr
    folder = host.bulk_root / "kopie"
    files = sorted(path.name for path in folder.iterdir())
    assert files == ["zufall-001.bin", "zufall-002.bin", "zufall-003.bin"]
    assert all((folder / name).stat().st_size == 1024 for name in files)
    assert "3 von 3 GiB geschrieben" in result.stdout
    assert "Fertig: 3 GiB Zufallsdaten" in result.stdout


def test_bulk_into_kopie2(host: Host) -> None:
    assert host.run("bulk", "1", str(host.bulk_root / "kopie2")).returncode == 0
    assert (host.bulk_root / "kopie2" / "zufall-001.bin").is_file()


@pytest.mark.parametrize("amount", ["0", "501", "-1", "1.5", "abc", "0001", ""])
def test_bulk_checks_the_amount(host: Host, amount: str) -> None:
    result = host.run("bulk", amount)
    assert result.returncode == 2
    assert "von 1 bis 500" in result.stderr
    assert not (host.bulk_root / "kopie").exists()


@pytest.mark.parametrize("folder", ["kopie3", "testdata", "kopie/unter", "../kopie"])
def test_bulk_checks_the_folder(host: Host, folder: str) -> None:
    result = host.run("bulk", "1", f"{host.bulk_root}/{folder}")
    assert result.returncode == 2
    assert "kopie oder" in result.stderr


def test_bulk_refuses_an_existing_folder(host: Host) -> None:
    (host.bulk_root / "kopie").mkdir()
    result = host.run("bulk", "1")
    assert result.returncode == 2
    assert "gibt es schon" in result.stderr


def test_bulk_checks_the_space(host: Host) -> None:
    # 100 GiB-Einheiten im Pool, 30 frei: danach müssen 20 frei bleiben, also höchstens 10.
    host.state["pool_used"] = 70 * 1024
    host.state["pool_available"] = 30 * 1024
    result = host.run("bulk", "11")
    assert result.returncode == 2
    assert "höchstens 10 GiB" in result.stderr
    assert not (host.bulk_root / "kopie").exists()
    assert host.run("bulk", "10").returncode == 0


def test_bulk_abort_removes_the_unfinished_file(host: Host) -> None:
    source = host.tmp / "random"
    source.unlink()
    os.mkfifo(source)
    host.write()
    process = subprocess.Popen(  # noqa: S603 - nur das Testskript aus dem Repo
        ["/bin/bash", str(host.script), "bulk", "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    unfinished = host.bulk_root / "kopie" / "zufall-001.bin"
    for _ in range(100):
        if unfinished.exists():
            break
        time.sleep(0.05)
    assert unfinished.exists()
    os.killpg(process.pid, signal.SIGINT)
    _stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 130
    assert "0 fertige Dateien bleiben" in stderr
    assert not unfinished.exists()
