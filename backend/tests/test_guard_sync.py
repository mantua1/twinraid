"""host-scripts/guard.sh Version 3, Modus sync: Spiegel auf Groß (Konzept 3.7, E26, E27).

Mit dem nachgebauten zfs aus docker/fake-target (tests/support/guard_host.py). Das
Verhalten von OpenZFS selbst prüft docker/guard-sync-test.sh mit Pools in Dateien.
"""

import fcntl
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tests.support.guard_host import (
    ALLOW_HEADER,
    ALLOW_LINES,
    FIXED,
    GIB,
    MOUNT_ROOT,
    POOL,
    RECV_PERMS,
    ROOT,
    Guard,
    kill_loops,
    make_guard,
    stream,
)

NAME = "twinraidtest"
MIRROR = f"{ROOT}/{NAME}"
DAY = 86400
MIRROR_LINE = (
    r"mirror [A-Za-z0-9][A-Za-z0-9._-]{0,63} state=(absent|mirror|foreign) "
    r"props=(ok|mismatch|-) count=(\d+|-|\?) newest_guid=(\d+|-|\?) "
    r"newest_created=(\d+|-|\?) newest_received=(\d+|-|\?) oldest_created=(\d+|-|\?)"
)
SUMMARY_LINE = (
    r"summary root=(ok|missing) conf=(ok|missing|invalid) deleg=(ok|mismatch|id-in-use) "
    r"receive=(idle|busy) clones=(\d+|\?) available=(\d+|\?) size=(\d+|\?) "
    r"reserve_percent=(\d+|-) keep_days=(\d+|-) min_interval_minutes=(\d+|-)"
)
# Die feste Befehlszeile aus Konzept 3.7, wie guard.sh sie an setpriv übergibt.
RECEIVE_LINE = [
    "receive",
    "-u",
    *[part for prop, value in FIXED.items() for part in ("-o", f"{prop}={value}")],
    "-o",
    f"mountpoint={MOUNT_ROOT}/{NAME}",
    "--",
    MIRROR,
]
SETPRIV = [
    "--reuid",
    "64000",
    "--regid",
    "64000",
    "--clear-groups",
    "--inh-caps=-all",
    "--bounding-set=-all",
    "--no-new-privs",
    "--",
]


@pytest.fixture
def guard(tmp_path: Path) -> Iterator[Guard]:
    yield make_guard(tmp_path)
    kill_loops(tmp_path)


def check(guard: Guard) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Führt check aus, prüft das Format streng und zerlegt die Zeilen."""
    result = guard.run("sync", "check")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert lines[0] == "twinraid-guard 3 sync ok"
    assert re.fullmatch(SUMMARY_LINE, lines[-1]), lines[-1]
    mirrors: dict[str, dict[str, str]] = {}
    for line in lines[1:-1]:
        assert re.fullmatch(MIRROR_LINE, line), line
        parts = line.split(" ")
        mirrors[parts[1]] = dict(part.split("=", 1) for part in parts[2:])
    summary = dict(part.split("=", 1) for part in lines[-1].split(" ")[1:])
    return mirrors, summary


def receive(guard: Guard, data: str, name: str = NAME) -> subprocess.CompletedProcess[str]:
    return guard.run("sync", f"receive {name}", stdin=data)


def refused(result: subprocess.CompletedProcess[str], reason: str) -> None:
    assert result.returncode == 3, (result.returncode, result.stderr)
    assert result.stderr == f"twinraid-guard: refused {reason}\n"
    assert result.stdout == ""


def received_by_zfs(guard: Guard) -> int:
    return sum(1 for call in guard.zfs_calls() if call[0] == "receive")


def locked(guard: Guard, dataset: str = MIRROR) -> bool:
    obj = guard.objects()[dataset]
    return guard.prop(dataset, "canmount") == ["noauto", "local"] and not obj["mounted"]


# --- check ---------------------------------------------------------------------------------


def test_check_without_mirrors(guard: Guard) -> None:
    mirrors, summary = check(guard)
    assert mirrors == {
        NAME: {
            "state": "absent",
            "props": "-",
            "count": "0",
            "newest_guid": "-",
            "newest_created": "-",
            "newest_received": "-",
            "oldest_created": "-",
        }
    }
    assert summary == {
        "root": "ok",
        "conf": "ok",
        "deleg": "ok",
        "receive": "idle",
        "clones": "0",
        "available": str(900 * GIB),
        "size": str(1000 * GIB),
        "reserve_percent": "15",
        "keep_days": "30",
        "min_interval_minutes": "10",
    }


def test_check_reports_a_mirror(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, 1_760_100_000), ("b", 12, 1_760_200_000)])
    mirrors, _ = check(guard)
    assert mirrors[NAME] == {
        "state": "mirror",
        "props": "ok",
        "count": "2",
        "newest_guid": "12",
        "newest_created": str(1_760_000_012),
        "newest_received": "1760200000",
        "oldest_created": str(1_760_000_011),
    }


def test_check_counts_only_local_received_times(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, None)])
    guard.set_prop(f"{MIRROR}@a", "twinraid:received", "1760000000", "received")
    mirrors, _ = check(guard)
    assert mirrors[NAME]["newest_received"] == "-"
    # Eine Eigenschaft aus dem Strom sperrt den Spiegel.
    assert mirrors[NAME]["props"] == "mismatch"


@pytest.mark.parametrize("variant", ["no-property", "received-property", "volume"])
def test_check_reports_a_foreign_dataset(guard: Guard, variant: str) -> None:
    if variant == "volume":
        guard.add_dataset(MIRROR, kind="volume")
    else:
        guard.add_dataset(MIRROR)
    if variant == "received-property":
        guard.set_prop(MIRROR, "twinraid:mirror", "1", "received")
    mirrors, _ = check(guard)
    assert mirrors[NAME]["state"] == "foreign"
    assert mirrors[NAME]["count"] == "-"
    assert mirrors[NAME]["props"] == "-"


@pytest.mark.parametrize(
    "change",
    [
        "canmount-noauto",
        "received-on-mirror",
        "received-on-snapshot",
        "child",
        "readonly-off",
        "exec-default",
        "mountpoint-elsewhere",
    ],
)
def test_check_reports_mismatching_properties(guard: Guard, change: str) -> None:
    guard.add_mirror(NAME, [("a", 11, 1_760_000_000)])
    match change:
        case "canmount-noauto":
            guard.set_prop(MIRROR, "canmount", "noauto")
        case "received-on-mirror":
            guard.set_prop(MIRROR, "special_small_blocks", "0", "received")
        case "received-on-snapshot":
            guard.set_prop(f"{MIRROR}@a", "twinraid:received", "1", "received")
        case "child":
            guard.add_dataset(f"{MIRROR}/kind")
        case "readonly-off":
            guard.set_prop(MIRROR, "readonly", "off")
        case "exec-default":
            guard.drop_prop(MIRROR, "exec")
        case "mountpoint-elsewhere":
            guard.set_prop(MIRROR, "mountpoint", "/etc")
    mirrors, _ = check(guard)
    assert mirrors[NAME]["props"] == "mismatch"
    # check verändert nichts, auch keine Sperre.
    assert guard.zfs_changes() == []


def test_check_accepts_compression_on(guard: Guard) -> None:
    guard.add_mirror(NAME)
    guard.set_prop(MIRROR, "compression", "on")
    mirrors, _ = check(guard)
    assert mirrors[NAME]["props"] == "ok"


def test_check_without_root(guard: Guard) -> None:
    guard.remove(ROOT)
    mirrors, summary = check(guard)
    assert mirrors[NAME]["state"] == "absent"
    assert summary["root"] == "missing"
    assert summary["deleg"] == "mismatch"


@pytest.mark.parametrize(
    ("text", "conf"),
    [(None, "missing"), ("mirror system\n", "invalid"), ("unknown 1\n", "invalid")],
)
def test_check_reports_the_state_of_mirror_conf(guard: Guard, text: str | None, conf: str) -> None:
    guard.mirror_conf(text)
    mirrors, summary = check(guard)
    assert mirrors == {}
    assert summary["conf"] == conf
    assert summary["reserve_percent"] == summary["keep_days"] == "-"
    assert summary["min_interval_minutes"] == "-"


def test_check_reports_a_running_receive(guard: Guard) -> None:
    lock = guard.lock_file()
    _, summary = check(guard)
    assert summary["receive"] == "idle"
    # check legt die Sperrdatei nicht an.
    assert not lock.exists()
    lock.touch()
    with lock.open() as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _, summary = check(guard)
    assert summary["receive"] == "busy"


def test_check_counts_clones_of_mirror_snapshots(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, 1_760_000_000)])
    guard.add_dataset(f"{POOL}/{NAME}")
    guard.change(f"{POOL}/{NAME}", origin=f"{MIRROR}@a")
    _, summary = check(guard)
    assert summary["clones"] == "1"


def test_check_changes_nothing(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, 1_760_000_000)], mounted=False)
    check(guard)
    assert guard.zfs_changes() == []
    assert guard.setpriv_calls() == []
    assert not guard.lock_file().exists()


def test_check_format_is_documented() -> None:
    concept = (REPO_ROOT / "docs" / "KONZEPT.md").read_text()
    assert (
        "mirror <Name> state=<absent|mirror|foreign> props=<ok|mismatch|-> count=<Anzahl> "
        "newest_guid=<GUID|-> newest_created=<Zeit|-> newest_received=<Zeit|-> "
        "oldest_created=<Zeit|->"
    ) in concept
    assert (
        "summary root=<ok|missing> conf=<ok|missing|invalid> deleg=<ok|mismatch|id-in-use> "
        "receive=<idle|busy> clones=<Anzahl|?> available=<Byte|?> size=<Byte|?> "
        "reserve_percent=<Zahl|-> keep_days=<Zahl|-> min_interval_minutes=<Zahl|->"
    ) in concept


# --- mirror.conf ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "names", "values"),
    [
        ("mirror twinraidtest\n", [NAME], ("15", "30", "10")),
        (
            "# Spiegel auf Groß\n\nmirror twinraidtest   # Test-Share\r\nmirror isos\n"
            "keep_days 60\nreserve_percent 5\nmin_interval_minutes 1440\n",
            [NAME, "isos"],
            ("5", "60", "1440"),
        ),
        (
            "  mirror\tappdata\t\nkeep_days 3650\nreserve_percent 50\n",
            ["appdata"],
            ("50", "3650", "10"),
        ),
        ("", [], ("15", "30", "10")),
    ],
)
def test_valid_mirror_conf(
    guard: Guard, text: str, names: list[str], values: tuple[str, str, str]
) -> None:
    guard.mirror_conf(text)
    mirrors, summary = check(guard)
    assert list(mirrors) == names
    assert (summary["reserve_percent"], summary["keep_days"]) == values[:2]
    assert summary["min_interval_minutes"] == values[2]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("mirror system\n", 1),
        ("mirror twinraidtest\nmirror Domains\n", 2),
        ("mirror SYSTEM\n", 1),
        ("mirror twinraidtest\nmirror twinraidtest\n", 2),
        ("mirror ../etc\n", 1),
        ("mirror a/b\n", 1),
        ("mirror -F\n", 1),
        ("mirror .hidden\n", 1),
        ("mirror " + "x" * 65 + "\n", 1),
        ("mirror a b\n", 1),
        ("mirror\n", 1),
        ("Mirror twinraidtest\n", 1),
        ("keep_days 29\n", 1),
        ("keep_days 3651\n", 1),
        ("reserve_percent 4\n", 1),
        ("reserve_percent 51\n", 1),
        ("min_interval_minutes 9\n", 1),
        ("min_interval_minutes 1441\n", 1),
        ("keep_days 30\nkeep_days 31\n", 2),
        ("keep_days -1\n", 1),
        ("keep_days 3x\n", 1),
        ("receive twinraidtest\n", 1),
        ("".join(f"mirror m{index}\n" for index in range(21)), 21),
        ("#\n" * 200 + "mirror twinraidtest\n", 201),
    ],
)
def test_invalid_mirror_conf_refuses_every_receive(guard: Guard, text: str, line: int) -> None:
    guard.mirror_conf(text)
    result = receive(guard, stream(("a", 11)))
    refused(result, "name-denied")
    assert received_by_zfs(guard) == 0
    assert f"mirror.conf is invalid (line {line})" in guard.syslog()


@pytest.mark.parametrize("variant", ["symlink", "directory", "owner"])
def test_mirror_conf_must_be_a_file_of_root(guard: Guard, tmp_path: Path, variant: str) -> None:
    path = guard.root / "boot/config/twinraid/mirror.conf"
    if variant == "symlink":
        other = tmp_path / "other.conf"
        other.write_text("mirror twinraidtest\n")
        path.unlink()
        path.symlink_to(other)
    elif variant == "directory":
        path.unlink()
        path.mkdir()
    else:
        other = tmp_path / "other-owner"
        other.mkdir()
        guard = make_guard(other, OWNER_UID=str(os.getuid() + 1))
    refused(receive(guard, stream(("a", 11))), "name-denied")
    assert "mirror.conf is invalid (line 0)" in guard.syslog()


# --- Anfragen ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        "receive",
        "receive ",
        "receive  twinraidtest",
        "receive twinraidtest ",
        "receive twinraidtest extra",
        "receive ../twinraidtest",
        "receive twinraid/test",
        "receive -F",
        "receive .twinraidtest",
        "receive twinraidtest;poweroff",
        "receive $(poweroff)",
        "receive twinraidtest\npoweroff",
        "receive " + "x" * 65,
        "Receive twinraidtest",
        "send twinraidtest",
        "destroy twinraidtest",
        "check twinraidtest",
    ],
)
def test_malformed_sync_requests_are_denied(guard: Guard, request_text: str) -> None:
    result = guard.run("sync", request_text, stdin=stream(("a", 11)))
    assert result.returncode == 2
    assert result.stderr == "twinraid-guard: denied\n"
    assert guard.zfs_calls() == []
    for message in guard.syslog():
        assert request_text not in message


@pytest.mark.parametrize("name", ["isos", "system", "domains", "Twinraidtest", "appdata"])
def test_names_outside_mirror_conf_are_refused(guard: Guard, name: str) -> None:
    result = receive(guard, stream(("a", 11)), name=name)
    refused(result, "name-denied")
    assert received_by_zfs(guard) == 0
    # Ein abgelehnter Name kommt nicht ins Syslog.
    assert all(name not in message for message in guard.syslog())


# --- Empfang -------------------------------------------------------------------------------


def test_first_receive_as_uid_64000_with_the_fixed_line(guard: Guard) -> None:
    before = int(time.time())
    result = receive(guard, stream(("twinraid-20261019T120000Z", 11)))
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert guard.setpriv_calls() == [
        [*SETPRIV, str(guard.script.parent / "bin/zfs"), *RECEIVE_LINE]
    ]
    # zfs receive läuft nur über setpriv.
    assert received_by_zfs(guard) == 1
    for prop, value in FIXED.items():
        assert guard.prop(MIRROR, prop) == [value, "local"], prop
    assert guard.prop(MIRROR, "mountpoint") == [f"{MOUNT_ROOT}/{NAME}", "local"]
    assert guard.objects()[MIRROR]["mounted"] is True
    stamp = guard.prop(f"{MIRROR}@twinraid-20261019T120000Z", "twinraid:received")
    assert stamp is not None
    assert stamp[1] == "local"
    assert before <= int(stamp[0]) <= int(time.time())
    log = guard.syslog()
    assert f"receive into mirror {NAME} started" in log
    assert f"receive into mirror {NAME} finished" in log


def test_incremental_receive(guard: Guard) -> None:
    old = int(time.time()) - 3600
    guard.add_mirror(NAME, [("a", 11, old)])
    result = receive(guard, stream(("b", 12), full=False, from_guid=11))
    assert result.returncode == 0, result.stderr
    assert guard.snapshots(MIRROR) == ["a", "b"]
    assert guard.prop(f"{MIRROR}@a", "twinraid:received") == [str(old), "local"]
    assert guard.prop(f"{MIRROR}@b", "twinraid:received")[1] == "local"  # type: ignore[index]
    assert guard.objects()[MIRROR]["mounted"] is True


def test_receive_lock(guard: Guard) -> None:
    lock = guard.lock_file()
    lock.touch()
    with lock.open() as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = receive(guard, stream(("a", 11)))
    refused(result, "receive-busy")
    assert received_by_zfs(guard) == 0


def test_receive_lock_must_not_be_a_symlink(guard: Guard, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    guard.lock_file().symlink_to(target)
    refused(receive(guard, stream(("a", 11))), "receive-busy")
    assert not target.exists()


def test_receive_needs_the_root(guard: Guard) -> None:
    guard.remove(ROOT)
    refused(receive(guard, stream(("a", 11))), "mirror-root-missing")
    assert received_by_zfs(guard) == 0


def test_receive_needs_a_filesystem_as_root(guard: Guard) -> None:
    guard.change(ROOT, type="volume")
    refused(receive(guard, stream(("a", 11))), "mirror-root-missing")


@pytest.mark.parametrize(
    ("passwd", "group"),
    [
        ("twinraid:x:64000:100::/:/bin/false\n", ""),
        ("other:x:1000:64000::/:/bin/false\n", ""),
        ("", "twinraid:x:64000:\n"),
        ("odd:x:064000:100::/:/bin/false\n", ""),
        ("last:x:64000:100::/:/bin/false", ""),
    ],
)
def test_receiver_ids_must_be_free(guard: Guard, passwd: str, group: str) -> None:
    with (guard.root / "etc/passwd").open("a") as handle:
        handle.write(passwd)
    with (guard.root / "etc/group").open("a") as handle:
        handle.write(group)
    refused(receive(guard, stream(("a", 11))), "receiver-id-in-use")
    assert received_by_zfs(guard) == 0
    _, summary = check(guard)
    assert summary["deleg"] == "id-in-use"


def test_ids_are_compared_as_numbers_not_as_text(guard: Guard) -> None:
    with (guard.root / "etc/passwd").open("a") as handle:
        handle.write("u64000:x:1000:1000:64000 64000:/home/64000:/bin/sh\n")
    with (guard.root / "etc/group").open("a") as handle:
        handle.write("g64000:x:1001:u64000\n")
    assert receive(guard, stream(("a", 11))).returncode == 0


@pytest.mark.parametrize("variant", ["no-property", "received-property", "volume"])
def test_foreign_dataset_is_never_replaced(guard: Guard, variant: str) -> None:
    guard.add_dataset(MIRROR, kind="volume" if variant == "volume" else "filesystem")
    if variant == "received-property":
        guard.set_prop(MIRROR, "twinraid:mirror", "1", "received")
    before = guard.objects()[MIRROR]
    refused(receive(guard, stream(("a", 11))), "target-exists")
    assert received_by_zfs(guard) == 0
    assert guard.objects()[MIRROR] == before


DELEGATION_VARIANTS = {
    "missing": None,
    "one-right-less": [
        *ALLOW_LINES[:2],
        "\tuser (unknown: 64000) " + RECV_PERMS.replace(",exec", ""),
    ],
    "one-right-more": [*ALLOW_LINES[:2], "\tuser (unknown: 64000) destroy," + RECV_PERMS],
    "duplicate-right": [*ALLOW_LINES[:2], "\tuser (unknown: 64000) exec," + RECV_PERMS],
    "other-order": [
        *ALLOW_LINES[:2],
        "\tuser (unknown: 64000) " + ",".join(reversed(RECV_PERMS.split(","))),
    ],
    "named-user": [*ALLOW_LINES[:2], "\tuser twinraid " + RECV_PERMS],
    "other-uid": [*ALLOW_LINES[:2], "\tuser (unknown: 64001) " + RECV_PERMS],
    "without-parentheses": [*ALLOW_LINES[:2], "\tuser unknown: 64000 " + RECV_PERMS],
    "spaces-not-tab": [*ALLOW_LINES[:2], "    user (unknown: 64000) " + RECV_PERMS],
    "group-too": [*ALLOW_LINES, "\tgroup (unknown: 64000) " + RECV_PERMS],
    "everyone-too": [*ALLOW_LINES, "\teveryone mount"],
    "local-only": [ALLOW_HEADER, "Local permissions:", ALLOW_LINES[2]],
    "descendent-only": [ALLOW_HEADER, "Descendent permissions:", ALLOW_LINES[2]],
    "local-too": [
        ALLOW_HEADER,
        "Local permissions:",
        "\tuser (unknown: 64000) destroy",
        *ALLOW_LINES[1:],
    ],
    "permission-set": [ALLOW_HEADER, "Permission sets:", "\t@set mount", *ALLOW_LINES[1:]],
    "create-time": [ALLOW_HEADER, "Create time permissions:", "\tdestroy", *ALLOW_LINES[1:]],
    "short-header": [f"---- Permissions on {ROOT} ---", *ALLOW_LINES[1:]],
    "blank-line": [ALLOW_HEADER, "", *ALLOW_LINES[1:]],
}


@pytest.mark.parametrize("variant", list(DELEGATION_VARIANTS))
def test_delegation_must_be_exact(guard: Guard, variant: str) -> None:
    lines = DELEGATION_VARIANTS[variant]
    guard.allow(lines)
    result = receive(guard, stream(("a", 11)))
    if variant == "other-order":
        # zfs allow sortiert selbst; verglichen wird die Menge.
        assert result.returncode == 0, result.stderr
        return
    refused(result, "delegation-mismatch")
    assert received_by_zfs(guard) == 0
    _, summary = check(guard)
    assert summary["deleg"] == "mismatch"


@pytest.mark.parametrize("where", ["pool", "mirror"])
def test_delegation_elsewhere_is_a_mismatch(guard: Guard, where: str) -> None:
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    dataset = POOL if where == "pool" else MIRROR
    header = f"---- Permissions on {dataset} ".ljust(70, "-")
    guard.allow([header, "Local+Descendent permissions:", "\tuser (unknown: 64000) mount"], dataset)
    result = receive(guard, stream(("b", 12), full=False, from_guid=11))
    refused(result, "delegation-mismatch")
    _, summary = check(guard)
    assert summary["deleg"] == "mismatch"


@pytest.mark.parametrize(
    "change",
    ["canmount-noauto", "received-on-mirror", "received-on-snapshot", "child"],
)
def test_locked_mirror_refuses_every_receive(guard: Guard, change: str) -> None:
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    match change:
        case "canmount-noauto":
            guard.set_prop(MIRROR, "canmount", "noauto")
        case "received-on-mirror":
            guard.set_prop(MIRROR, "quota", "1G", "received")
        case "received-on-snapshot":
            guard.set_prop(f"{MIRROR}@a", "twinraid:received", "1", "received")
        case "child":
            guard.add_dataset(f"{MIRROR}/kind")
    refused(receive(guard, stream(("b", 12), full=False, from_guid=11)), "props-mismatch")
    assert received_by_zfs(guard) == 0


@pytest.mark.parametrize(
    "change", ["readonly-off", "exec-default", "setuid-default", "mountpoint-elsewhere"]
)
def test_fixed_values_are_checked_before_each_receive(guard: Guard, change: str) -> None:
    """Das Recht readonly erlaubt auch zfs set readonly=off (Konzept 3.7)."""
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    match change:
        case "readonly-off":
            guard.set_prop(MIRROR, "readonly", "off")
        case "exec-default":
            guard.drop_prop(MIRROR, "exec")
        case "setuid-default":
            guard.drop_prop(MIRROR, "setuid")
        case "mountpoint-elsewhere":
            guard.set_prop(MIRROR, "mountpoint", "/mnt/user/appdata")
    result = receive(guard, stream(("b", 12), full=False, from_guid=11))
    refused(result, "props-not-applied")
    assert received_by_zfs(guard) == 0
    assert locked(guard)
    assert f"mirror {NAME} locked" in guard.syslog()
    # Danach bleibt der Spiegel gesperrt, bis root ihn von Hand zurücksetzt.
    refused(receive(guard, stream(("b", 12), full=False, from_guid=11)), "props-mismatch")


def test_compression_on_counts_as_lz4(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    guard.set_prop(MIRROR, "compression", "on")
    assert receive(guard, stream(("b", 12), full=False, from_guid=11)).returncode == 0


def test_clone_of_a_mirror_snapshot_blocks_every_receive(guard: Guard) -> None:
    guard.mirror_conf("mirror twinraidtest\nmirror isos\n")
    guard.add_mirror("isos", [("a", 21, int(time.time()) - DAY)])
    guard.add_dataset(f"{POOL}/isos")
    guard.change(f"{POOL}/isos", origin=f"{ROOT}/isos@a")
    refused(receive(guard, stream(("a", 11))), "clone-active")
    assert received_by_zfs(guard) == 0


def test_clones_of_other_snapshots_do_not_count(guard: Guard) -> None:
    guard.add_dataset(f"{ROOT}/zfs-check")
    guard.add_dataset(f"{ROOT}/zfs-check@x", kind="snapshot", guid="5", creation="1")
    guard.add_dataset(f"{POOL}/klon")
    guard.change(f"{POOL}/klon", origin=f"{ROOT}/zfs-check@x")
    assert receive(guard, stream(("a", 11))).returncode == 0


@pytest.mark.parametrize(
    ("available_gib", "reserve", "accepted"),
    [(150, None, True), (149, None, False), (60, "5", True), (40, "5", False)],
)
def test_space_reserve(
    guard: Guard, available_gib: int, reserve: str | None, accepted: bool
) -> None:
    if reserve is not None:
        guard.mirror_conf(f"mirror twinraidtest\nreserve_percent {reserve}\n")
    guard.set_prop(ROOT, "available", str(available_gib * GIB), "-")
    result = receive(guard, stream(("a", 11)))
    if accepted:
        assert result.returncode == 0, result.stderr
    else:
        refused(result, "space-low")
        assert received_by_zfs(guard) == 0


@pytest.mark.parametrize(
    ("age_seconds", "source", "conf", "accepted"),
    [
        (60, "local", "", False),
        (580, "local", "", False),
        (601, "local", "", True),
        (1500, "local", "min_interval_minutes 30\n", False),
        (1801, "local", "min_interval_minutes 30\n", True),
        # Nur Quelle local zählt; ein Wert in der Zukunft sperrt nicht.
        (60, "received", "", None),
        (-3600, "local", "", True),
    ],
)
def test_minimum_interval_per_mirror(
    guard: Guard, age_seconds: int, source: str, conf: str, accepted: bool | None
) -> None:
    guard.mirror_conf(f"mirror twinraidtest\n{conf}")
    guard.add_mirror(NAME, [("a", 11, None)])
    guard.set_prop(f"{MIRROR}@a", "twinraid:received", str(int(time.time()) - age_seconds), source)
    result = receive(guard, stream(("b", 12), full=False, from_guid=11))
    if accepted is None:
        # Eine Eigenschaft aus dem Strom sperrt vorher.
        refused(result, "props-mismatch")
    elif accepted:
        assert result.returncode == 0, result.stderr
    else:
        refused(result, "too-soon")
        assert received_by_zfs(guard) == 0


# --- Prüfung nach dem Empfang -----------------------------------------------------------------


def test_stream_with_properties_locks_the_mirror(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    hostile = stream(("b", 12), full=False, from_guid=11, props={"special_small_blocks": "128K"})
    refused(receive(guard, hostile), "received-properties")
    assert locked(guard)
    assert guard.prop(f"{MIRROR}@b", "twinraid:received") is None
    assert "refused: received-properties" in guard.syslog()


def test_full_stream_with_properties_is_never_mounted(guard: Guard) -> None:
    hostile = stream(("a", 11), props={"overlay": "on"})
    refused(receive(guard, hostile), "received-properties")
    assert locked(guard)
    assert not any(call[0] == "mount" for call in guard.zfs_calls())


def test_stream_with_children_locks_mirror_and_children(guard: Guard) -> None:
    """Ein Strom von zfs send -R bringt Kinder mit; canmount wird nicht vererbt."""
    refused(receive(guard, stream(("a", 11), children=["etc", "boot"])), "received-properties")
    assert locked(guard)
    assert locked(guard, f"{MIRROR}/etc")
    assert locked(guard, f"{MIRROR}/boot")
    mirrors, _ = check(guard)
    assert mirrors[NAME]["props"] == "mismatch"


def test_stream_of_a_volume_is_locked(guard: Guard) -> None:
    refused(receive(guard, stream(("a", 11), volume=True)), "props-not-applied")
    assert guard.prop(MIRROR, "volmode") == ["none", "local"]
    mirrors, _ = check(guard)
    assert mirrors[NAME]["state"] == "foreign"


@pytest.mark.parametrize("right", ["exec", "readonly", "mountpoint", "canmount", "twinraid:mirror"])
def test_missing_right_locks_the_mirror(guard: Guard, right: str) -> None:
    """Ohne Recht übergeht zfs receive das -o mit Exit-Code 0 (Z1b)."""
    guard.deny_rights(right)
    result = receive(guard, stream(("a", 11)))
    refused(result, "props-not-applied")
    assert locked(guard)
    assert not any(call[0] == "mount" for call in guard.zfs_calls())
    assert f"zfs receive: cannot receive {right} property on {MIRROR}: permission denied" in (
        guard.syslog()
    )
    # Die Meldung von zfs geht nie an Klein.
    assert "permission denied" not in result.stderr


def test_missing_right_on_an_incremental_receive(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    guard.deny_rights("exec")
    # Der Wert stand schon local am Spiegel; ohne Recht bleibt er stehen.
    assert receive(guard, stream(("b", 12), full=False, from_guid=11)).returncode == 0
    guard.drop_prop(MIRROR, "exec")
    guard.set_prop(f"{MIRROR}@b", "twinraid:received", str(int(time.time()) - DAY))
    refused(receive(guard, stream(("c", 13), full=False, from_guid=12)), "props-not-applied")


def test_failed_receive_changes_nothing_and_says_nothing(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    result = receive(guard, stream(("b", 12), full=False, from_guid=99))
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""
    assert guard.snapshots(MIRROR) == ["a"]
    assert guard.objects()[MIRROR]["mounted"] is True
    assert (
        f"zfs receive: cannot receive incremental stream: most recent snapshot of {MIRROR} does not"
    ) in guard.syslog()
    assert f"failed: receive into mirror {NAME} (zfs exit code 1)" in guard.syslog()


def test_failed_first_receive_leaves_no_mirror(guard: Guard) -> None:
    result = receive(guard, stream(("a", 11), error="cannot receive: invalid stream"))
    assert result.returncode == 1
    assert MIRROR not in guard.objects()


def test_partly_received_snapshots_are_checked_and_marked(guard: Guard) -> None:
    """Ein Strom mit mehreren Snapshots (-I) kann einen Teil angelegt haben."""
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    partial = stream(("b", 12), ("c", 13), full=False, from_guid=11, fail_after=1)
    result = receive(guard, partial)
    assert result.returncode == 1
    assert guard.snapshots(MIRROR) == ["a", "b"]
    assert guard.prop(f"{MIRROR}@b", "twinraid:received")[1] == "local"  # type: ignore[index]
    assert guard.objects()[MIRROR]["mounted"] is True


def test_modified_target_is_named(guard: Guard) -> None:
    guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
    guard.change(MIRROR, modified=True)
    refused(receive(guard, stream(("b", 12), full=False, from_guid=11)), "target-modified")
    assert guard.snapshots(MIRROR) == ["a"]


def test_older_base_counts_as_modified_target(guard: Guard) -> None:
    """Eine ältere vorhandene Basis meldet OpenZFS wie ein verändertes Ziel (Konzept 3.7)."""
    day_ago = int(time.time()) - DAY
    guard.add_mirror(NAME, [("a", 11, day_ago), ("b", 12, day_ago)])
    refused(receive(guard, stream(("c", 13), full=False, from_guid=11)), "target-modified")
    assert guard.snapshots(MIRROR) == ["a", "b"]


@pytest.mark.parametrize("variant", ["elsewhere", "mount-fails"])
def test_mount_is_checked(guard: Guard, variant: str) -> None:
    if variant == "elsewhere":
        guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)])
        guard.change(MIRROR, mounted_at="/mnt/user/appdata")
        result = receive(guard, stream(("b", 12), full=False, from_guid=11))
    else:
        guard.add_mirror(NAME, [("a", 11, int(time.time()) - DAY)], mounted=False)
        guard.change(MIRROR, mount_fails=True)
        result = receive(guard, stream(("b", 12), full=False, from_guid=11))
    refused(result, "mount-mismatch")
    assert guard.objects()[MIRROR]["mounted"] is False
    # Kein Fund in den Eigenschaften: keine Sperre, der nächste Empfang darf es erneut versuchen.
    assert guard.prop(MIRROR, "canmount") == ["on", "local"]


def test_message_of_zfs_is_cleaned_and_shortened(guard: Guard) -> None:
    message = "cannot receive: \x1b[31mbad\tstream äöü " + "x" * 300
    result = receive(guard, stream(("a", 11), error=message))
    assert result.returncode == 1
    entries = [entry for entry in guard.syslog() if entry.startswith("zfs receive: ")]
    assert len(entries) == 1
    text = entries[0].removeprefix("zfs receive: ")
    assert len(text) == 200
    assert text.startswith("cannot receive: ?[31mbad?stream ?????? x")
    assert all(" " <= char <= "~" for char in text)


def test_hangup_does_not_skip_the_checks(guard: Guard) -> None:
    """Bricht die Verbindung ab, laufen die Prüfungen nach dem Empfang trotzdem."""
    env = {**guard.env(), "SSH_ORIGINAL_COMMAND": f"receive {NAME}"}
    process = subprocess.Popen(  # noqa: S603 - feste Argumente, nur im Test
        ["/bin/bash", str(guard.script), "sync"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while received_by_zfs(guard) == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert received_by_zfs(guard) == 1
    process.send_signal(signal.SIGHUP)
    time.sleep(0.2)
    _, stderr = process.communicate(stream(("a", 11), props={"overlay": "on"}), timeout=10)
    assert process.returncode == 3
    assert stderr == "twinraid-guard: refused received-properties\n"
    assert locked(guard)


# --- Aufbewahrung --------------------------------------------------------------------------


def test_retention_after_a_receive(guard: Guard) -> None:
    now = int(time.time())
    old = now - 40 * DAY
    guard.add_mirror(
        NAME,
        [
            ("s1", 1, old),
            ("s2", 2, None),
            ("s3", 3, old),
            ("s4", 4, now - 20 * DAY),
            ("s5", 5, old),
            ("s6", 6, old),
            ("s7", 7, now - DAY),
        ],
    )
    guard.change(f"{MIRROR}@s3", holds=["twinraid-base"])
    assert receive(guard, stream(("s8", 8), full=False, from_guid=7)).returncode == 0
    # s1 und s5 sind alt; s2 trägt kein twinraid:received mit Quelle local, s3 hat einen
    # Hold, s4 ist jünger als 30 Tage; s6, s7 und s8 sind die drei neuesten.
    assert guard.snapshots(MIRROR) == ["s2", "s3", "s4", "s6", "s7", "s8"]
    assert f"retention of mirror {NAME}: 2 snapshots destroyed, 1 failed" in guard.syslog()
    destroyed = [call for call in guard.zfs_calls() if call[0] == "destroy"]
    assert destroyed == [
        ["destroy", "--", f"{MIRROR}@s1"],
        ["destroy", "--", f"{MIRROR}@s3"],
        ["destroy", "--", f"{MIRROR}@s5"],
    ]


def test_retention_respects_keep_days(guard: Guard) -> None:
    guard.mirror_conf("mirror twinraidtest\nkeep_days 60\n")
    old = int(time.time()) - 40 * DAY
    guard.add_mirror(NAME, [("s1", 1, old), ("s2", 2, old), ("s3", 3, old), ("s4", 4, old)])
    assert receive(guard, stream(("s5", 5), full=False, from_guid=4)).returncode == 0
    assert guard.snapshots(MIRROR) == ["s1", "s2", "s3", "s4", "s5"]


def test_retention_covers_every_mirror_in_order(guard: Guard) -> None:
    guard.mirror_conf("mirror twinraidtest\nmirror isos\nmirror appdata\n")
    old = int(time.time()) - 40 * DAY
    four = [("s1", 1, old), ("s2", 2, old), ("s3", 3, old), ("s4", 4, old)]
    guard.add_mirror("isos", four)
    guard.add_mirror("appdata", four)
    guard.set_prop(f"{ROOT}/appdata", "canmount", "noauto")
    guard.add_mirror("fremd", four)
    guard.drop_prop(f"{ROOT}/fremd", "twinraid:mirror")
    assert receive(guard, stream(("a", 11))).returncode == 0
    assert guard.snapshots(f"{ROOT}/isos") == ["s2", "s3", "s4"]
    # Ein gesperrter Spiegel und Datasets außerhalb von mirror.conf bleiben unberührt.
    assert guard.snapshots(f"{ROOT}/appdata") == ["s1", "s2", "s3", "s4"]
    assert guard.snapshots(f"{ROOT}/fremd") == ["s1", "s2", "s3", "s4"]


def test_no_retention_after_a_failed_receive(guard: Guard) -> None:
    old = int(time.time()) - 40 * DAY
    guard.add_mirror(NAME, [("s1", 1, old), ("s2", 2, old), ("s3", 3, old), ("s4", 4, old)])
    assert receive(guard, stream(("s5", 5), full=False, from_guid=99)).returncode == 1
    assert guard.snapshots(MIRROR) == ["s1", "s2", "s3", "s4"]
    assert not any(call[0] == "destroy" for call in guard.zfs_calls())


# --- Skript --------------------------------------------------------------------------------


def test_repository_script_has_the_fixed_receive_line() -> None:
    text = (REPO_ROOT / "host-scripts" / "guard.sh").read_text()
    assert "readonly POOL=mainpool\n" in text
    assert "readonly MIRROR_ROOT=mainpool/twinraid-mirror\n" in text
    assert "readonly MOUNT_ROOT=/mnt/mainpool/twinraid-mirror\n" in text
    assert "readonly MIRROR_CONF=/boot/config/twinraid/mirror.conf\n" in text
    assert "readonly OWNER_UID=0\n" in text
    assert "readonly PASSWD=/etc/passwd\n" in text
    assert "readonly GROUP=/etc/group\n" in text
    assert "readonly RECV_UID=64000\n" in text
    assert "readonly RECV_GID=64000\n" in text
    assert f"readonly RECV_PERMS={RECV_PERMS}\n" in text
    assert "readonly RECEIVED_ALLOWED=()\n" in text
    # Nie -F, nie zfs destroy mit -r oder -R (CLAUDE.md, Regel 7); Kommentare zählen nicht.
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in (" -F", "destroy -r", "destroy -R", "eval "):
        assert forbidden not in code, forbidden
    # Die feste Befehlszeile kennt nur -u und -o; die vollständige Zeile prüft
    # test_first_receive_as_uid_64000_with_the_fixed_line.
    block = text.split("readonly RECEIVE_OPTIONS=(\n", 1)[1].split("\n)\n", 1)[0]
    flags = {word for word in block.split() if word.startswith("-")}
    assert flags == {"-u", "-o"}
    assert sorted(RECV_PERMS.split(",")) == RECV_PERMS.split(",")
    assert len(RECV_PERMS.split(",")) == 28


def test_rights_are_the_ones_from_the_concept() -> None:
    concept = (REPO_ROOT / "docs" / "KONZEPT.md").read_text()
    listed = ", ".join(f"`{right}`" for right in RECV_PERMS.split(","))
    assert listed in concept
    assert "user (unknown: 64000)" in concept
