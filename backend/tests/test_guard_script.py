"""host-scripts/guard.sh Version 3, Modi status und power (Konzept 3.5 und 5.6).

Den Testaufbau (Kopie des Skripts mit Testpfaden, Nachbauten von virsh, zfs und
findmnt) liefert tests/support/guard_host.py; den Modus sync prüft test_guard_sync.py.
"""

import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tests.support.guard_host import (
    GIB,
    GUARD,
    OTHER_UUID,
    UUID,
    Guard,
    hex_of,
    kill_loops,
    make_guard,
    processes_of,
)


@pytest.fixture
def guard(tmp_path: Path) -> Iterator[Guard]:
    yield make_guard(tmp_path)
    kill_loops(tmp_path)


# --- check -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["status", "power"])
def test_check_reports_version_and_does_nothing(guard: Guard, mode: str) -> None:
    result = guard.run(mode, "check")
    assert result.returncode == 0
    assert result.stdout == f"twinraid-guard 3 {mode} ok\n"
    assert guard.virsh_calls() == []
    assert guard.poweroffs() == 0


# --- Aktionen nur für die VM aus power.conf ------------------------------------------------


@pytest.mark.parametrize(
    ("request_text", "command", "message"),
    [
        ("vm-start", "start", f"vm-start of VM {UUID} requested by TwinRaid"),
        ("vm-shutdown", "shutdown", f"ACPI shutdown of VM {UUID} requested by TwinRaid"),
        ("vm-force-stop", "destroy", f"forced stop of VM {UUID} requested by TwinRaid"),
    ],
)
def test_vm_commands_use_the_uuid_from_power_conf(
    guard: Guard, request_text: str, command: str, message: str
) -> None:
    if command != "start":
        guard.set_vms((UUID, "Gaming VM", "running"))
    result = guard.run("power", request_text)
    assert result.returncode == 0
    assert guard.virsh_actions() == [f"{command} --domain {UUID}"]
    assert message in guard.syslog()
    # Ausgaben von virsh gehen nie an TwinRaid.
    assert result.stdout == ""
    assert result.stderr == ""


def test_vm_start_changes_only_the_configured_vm(guard: Guard) -> None:
    assert guard.run("power", "vm-start").returncode == 0
    assert guard.vm_states() == {UUID: "running", OTHER_UUID: "shut off"}


def test_failing_virsh_is_reported_with_its_exit_code(guard: Guard) -> None:
    guard.set_vms((UUID, "Gaming VM", "running"))
    result = guard.run("power", "vm-start")
    assert result.returncode == 1
    assert result.stderr == ""


def test_hanging_virsh_action_is_stopped(guard: Guard) -> None:
    guard.flag("virsh-hang")
    started = time.monotonic()
    result = guard.run("power", "vm-shutdown")
    assert result.returncode == 124
    assert time.monotonic() - started < 10


# --- vm-start: Speicher und belegtes Gerät (ab Version 3, Konzept 3.5, E25e) ----------------

KIB_PER_GIB = 1024 * 1024


@pytest.mark.parametrize(
    ("available_gib", "vm_gib", "arc_gib", "starts"),
    [
        (16, 0.5, None, True),
        # Genau die VM plus 1 GiB Abstand reicht.
        (5, 4, None, True),
        (4.999, 4, None, False),
        # Der ARC über c_min zählt mit; MemAvailable enthält ihn nicht (Z1).
        (3, 4, (6, 4), True),
        (3, 4, (5, 4), False),
        # Unter c_min gibt ZFS nichts ab; abgezogen wird aber auch nichts.
        (5, 4, (1, 2), True),
        (4, 4, (1, 2), False),
    ],
)
def test_vm_start_checks_free_memory(
    guard: Guard,
    available_gib: float,
    vm_gib: float,
    arc_gib: tuple[int, int] | None,
    starts: bool,
) -> None:
    guard.meminfo(int(available_gib * KIB_PER_GIB))
    guard.set_vms((UUID, "Gaming VM", "shut off"), memory_kib=int(vm_gib * KIB_PER_GIB))
    if arc_gib is not None:
        size, c_min = arc_gib
        guard.zfs_module("0", size=size * GIB, c_min=c_min * GIB, c_max=8 * GIB)
    result = guard.run("power", "vm-start")
    if starts:
        assert result.returncode == 0
        assert guard.virsh_actions() == [f"start --domain {UUID}"]
    else:
        assert result.returncode == 3
        assert result.stderr == "twinraid-guard: refused memory-low\n"
        assert guard.virsh_actions() == []
        assert "refused: memory-low" in guard.syslog()


def test_vm_start_without_mem_available_is_refused(guard: Guard) -> None:
    guard.meminfo(None)
    result = guard.run("power", "vm-start")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused memory-low\n"
    assert guard.virsh_actions() == []


@pytest.mark.parametrize("flag", ["libvirt-down", "virsh-hang"])
def test_vm_start_without_vm_information_is_refused(guard: Guard, flag: str) -> None:
    guard.flag(flag)
    started = time.monotonic()
    result = guard.run("power", "vm-start")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused vm-state-unknown\n"
    assert not any(call.startswith("start ") for call in guard.virsh_calls())
    assert time.monotonic() - started < 10


def test_vm_start_reports_a_device_in_use(guard: Guard) -> None:
    guard.flag("device-busy")
    result = guard.run("power", "vm-start")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused device-busy\n"
    assert "refused: device-busy" in guard.syslog()
    # Die Meldung von virsh geht weder an TwinRaid noch ins Syslog.
    assert result.stdout == ""
    for message in guard.syslog():
        assert "in use" not in message
        assert "0000:01:00.0" not in message


@pytest.mark.parametrize("request_text", ["vm-start", "vm-shutdown", "vm-force-stop"])
def test_vm_commands_are_refused_without_power_conf(guard: Guard, request_text: str) -> None:
    guard.configure(None)
    result = guard.run("power", request_text)
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused vm-not-configured\n"
    assert guard.virsh_calls() == []
    assert "refused: vm-not-configured" in guard.syslog()


@pytest.mark.parametrize(
    "text",
    [
        f"vm {UUID}",
        f"# Gaming-VM\n\nvm {UUID}\n",
        f"vm {UUID}\r\n",
        f"vm {UUID.upper()}\n",
    ],
)
def test_valid_power_conf(guard: Guard, text: str) -> None:
    guard.configure(text)
    assert guard.run("power", "vm-start").returncode == 0
    assert guard.virsh_actions() == [f"start --domain {UUID}"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# nur ein Kommentar\n",
        f" vm {UUID}\n",
        f"vm {UUID} \n",
        f"vm  {UUID}\n",
        f"VM {UUID}\n",
        f"vm {UUID}\nvm {OTHER_UUID}\n",
        f"vm {UUID}\nvm {UUID}\n",
        f"vm {UUID}\nextra\n",
        f"uuid {UUID}\n",
        "vm Gaming VM\n",
        f"vm {UUID[:-1]}\n",
        f"vm {UUID}0\n",
        "vm 0b5c4e1a2f3d4c5b8a9e112233445566aaaa\n",
        "vm $(poweroff)\n",
        "\n" * 60 + f"vm {UUID}\n",
    ],
)
def test_invalid_power_conf_is_refused(guard: Guard, text: str) -> None:
    guard.configure(text)
    result = guard.run("power", "vm-start")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused vm-not-configured\n"
    assert guard.virsh_actions() == []
    assert guard.poweroffs() == 0


def test_power_conf_must_not_be_a_symlink(guard: Guard, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.conf"
    real.write_text(f"vm {UUID}\n")
    guard.configure(None)
    guard.power_conf.symlink_to(real)
    assert guard.run("power", "vm-start").returncode == 3
    assert guard.virsh_actions() == []


# --- Ablehnungen -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    ["vm-start", "vm-shutdown", "vm-force-stop", "poweroff", f"vm-shutdown {UUID}"],
)
def test_status_mode_cannot_change_anything(guard: Guard, request_text: str) -> None:
    guard.set_vms((UUID, "Gaming VM", "running"))
    result = guard.run("status", request_text)
    assert result.returncode == 2
    assert result.stderr == "twinraid-guard: denied\n"
    assert guard.virsh_calls() == []
    assert guard.poweroffs() == 0


def test_power_mode_has_no_watch(guard: Guard) -> None:
    assert guard.run("power", "vm-watch").returncode == 2
    assert guard.virsh_calls() == []


@pytest.mark.parametrize("mode", ["status", "power", "sync"])
@pytest.mark.parametrize(
    "request_text",
    [
        "",
        "Check",
        "check ",
        " check",
        " poweroff",
        "poweroff now",
        "poweroff; rm -rf /",
        "check && poweroff",
        "reboot",
        "rm -rf /",
        "vm-start ",
        f"vm-start {UUID}",
        f"vm-shutdown {UUID}",
        f"vm-shutdown {UUID};poweroff",
        f"vm-shutdown\n{UUID}",
        "vm-shutdown\npoweroff",
        f"vm-force-stop {UUID}",
        "vm-force-stop --domain x",
        "vm-watch ",
        "vm-watch extra",
        "vm-watch\npoweroff",
        "vm-destroy",
        "vm-shutdown $(poweroff)",
        "rsync --server -logDtpre.iLsfxC . /mnt/user/appdata",
    ],
)
def test_everything_else_is_denied(guard: Guard, mode: str, request_text: str) -> None:
    guard.set_vms((UUID, "Gaming VM", "running"))
    result = guard.run(mode, request_text)
    assert result.returncode == 2
    assert result.stderr == "twinraid-guard: denied\n"
    assert guard.virsh_calls() == []
    assert guard.poweroffs() == 0
    # Das Syslog bekommt nur feste Texte, nie die Anfrage selbst.
    for message in guard.syslog():
        if request_text.strip():
            assert request_text not in message


def test_missing_request_is_denied(guard: Guard) -> None:
    assert guard.run("power", None).returncode == 2
    assert guard.run("status", None).returncode == 2
    assert guard.virsh_calls() == []


@pytest.mark.parametrize("mode", ["", "Power", "Status", "Sync", "power extra", "sync check", None])
def test_other_modes_are_denied(guard: Guard, mode: str | None) -> None:
    for request_text in ("check", "poweroff", "vm-start", "vm-watch"):
        assert guard.run(mode, request_text).returncode == 2
    assert guard.virsh_calls() == []
    assert guard.poweroffs() == 0


# --- poweroff nur, wenn alle VMs aus sind --------------------------------------------------------


def test_poweroff_when_all_vms_are_off(guard: Guard) -> None:
    result = guard.run("power", "poweroff")
    assert result.returncode == 0
    assert guard.poweroffs() == 1
    assert "poweroff requested by TwinRaid" in guard.syslog()
    assert guard.virsh_actions() == []


def test_poweroff_allows_a_crashed_gaming_vm(guard: Guard) -> None:
    guard.set_vms((UUID, "Gaming VM", "crashed"), (OTHER_UUID, "Second VM", "shut off"))
    assert guard.run("power", "poweroff").returncode == 0
    assert guard.poweroffs() == 1


@pytest.mark.parametrize("state", ["running", "idle", "paused", "in shutdown", "pmsuspended"])
def test_poweroff_is_refused_while_the_gaming_vm_runs(guard: Guard, state: str) -> None:
    guard.set_vms((UUID, "Gaming VM", state), (OTHER_UUID, "Second VM", "shut off"))
    result = guard.run("power", "poweroff")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused vm-running\n"
    assert guard.poweroffs() == 0
    assert "refused: vm-running" in guard.syslog()


@pytest.mark.parametrize("state", ["running", "paused", "crashed", "in shutdown", "no state"])
def test_poweroff_is_refused_while_another_vm_runs(guard: Guard, state: str) -> None:
    guard.set_vms((UUID, "Gaming VM", "shut off"), (OTHER_UUID, "Second VM", state))
    result = guard.run("power", "poweroff")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused other-vms-running\n"
    assert guard.poweroffs() == 0


@pytest.mark.parametrize("flag", ["libvirt-down", "virsh-hang"])
def test_poweroff_is_refused_when_the_vm_list_is_unreadable(guard: Guard, flag: str) -> None:
    guard.flag(flag)
    result = guard.run("power", "poweroff")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused vm-state-unknown\n"
    assert guard.poweroffs() == 0


def test_poweroff_without_power_conf_needs_every_vm_off(guard: Guard) -> None:
    guard.configure(None)
    assert guard.run("power", "poweroff").returncode == 0
    guard.set_vms((UUID, "Gaming VM", "crashed"))
    result = guard.run("power", "poweroff")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused other-vms-running\n"
    assert guard.poweroffs() == 1


def test_poweroff_is_refused_during_a_receive(guard: Guard) -> None:
    lock = guard.lock_file()
    lock.touch()
    with lock.open() as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = guard.run("power", "poweroff")
    assert result.returncode == 3
    assert result.stderr == "twinraid-guard: refused sync-running\n"
    assert guard.poweroffs() == 0
    assert "refused: sync-running" in guard.syslog()


def test_poweroff_keeps_the_receive_lock(tmp_path: Path) -> None:
    """poweroff erbt die Sperre: Danach beginnt kein Empfang mehr (Konzept 3.7)."""
    probe = tmp_path / "probe.sh"
    result_file = tmp_path / "probe.out"
    lock = tmp_path / "root/run/twinraid-guard-receive.lock"
    probe.write_text(
        f"#!/bin/sh\nif flock -n {lock} true; then echo free; else echo held; fi >{result_file}\n"
    )
    probe.chmod(0o755)
    guard = make_guard(tmp_path, POWEROFF=str(probe))
    assert guard.run("power", "poweroff").returncode == 0
    assert result_file.read_text() == "held\n"
    assert lock.is_file()


# --- vm-watch ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("running", "RUNNING"),
        ("idle", "IDLE"),
        ("paused", "PAUSED"),
        ("in shutdown", "SHUTDOWN"),
        ("shut off", "SHUTOFF"),
        ("crashed", "CRASHED"),
        ("pmsuspended", "PMSUSPENDED"),
        ("no state", "NOSTATE"),
        ("something new", "NOSTATE"),
    ],
)
def test_watch_reports_every_state(guard: Guard, state: str, expected: str) -> None:
    guard.set_vms((UUID, "Gaming VM", state))
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert line == (
        f"watch 3 vm={expected} name={hex_of('Gaming VM')} others=0 names=- "
        "connect=absent flash_backup=inactive arc_max=- c_max=-"
    )


def test_watch_lists_other_running_vms_hex_encoded(guard: Guard) -> None:
    names = ["Second VM", "Zweite Größe", "tab\tand\x1bescape", "plain"]
    uuids = [f"7c6b5a49-3827-4161-9504-f3e2d1c0b9a{index}" for index in range(4)]
    guard.set_vms(
        (UUID, "Gaming VM", "running"),
        (uuids[0], names[0], "running"),
        (uuids[1], names[1], "paused"),
        (uuids[2], names[2], "crashed"),
        (uuids[3], names[3], "shut off"),
    )
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    expected = ",".join(hex_of(name) for name in names[:3])
    assert f" others=3 names={expected} " in line
    line.encode("ascii")


def test_watch_limits_names(guard: Guard) -> None:
    long_name = "x" * 300
    vms = [(UUID, "Gaming VM", "shut off")]
    vms += [
        (f"00000000-0000-4000-8000-0000000000{index:02d}", long_name, "running")
        for index in range(25)
    ]
    guard.set_vms(*vms)
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    names = line.split(" names=")[1].split(" ")[0].split(",")
    assert " others=25 " in line
    assert len(names) == 20
    assert all(name == hex_of("x" * 256) for name in names)


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("libvirt-down", "vm=LIBVIRT_DOWN"), ("virsh-hang", "vm=UNKNOWN")],
)
def test_watch_without_vm_service(guard: Guard, flag: str, expected: str) -> None:
    guard.flag(flag)
    watch = guard.start_watch()
    line = watch.line(timeout=10)
    watch.close()
    assert line.startswith(f"watch 3 {expected} ")
    assert " others=? names=- " in line


def test_watch_keeps_running_after_virsh_hung(guard: Guard) -> None:
    guard.set_vms((UUID, "Gaming VM", "running"))
    guard.flag("virsh-hang")
    watch = guard.start_watch()
    watch.line_with("vm=UNKNOWN", timeout=10)
    guard.flag("virsh-hang", on=False)
    watch.line_with("vm=RUNNING", timeout=10)
    watch.close()


def test_watch_without_power_conf(guard: Guard) -> None:
    guard.configure(None)
    guard.set_vms((UUID, "Gaming VM", "running"))
    watch = guard.start_watch()
    first = watch.line()
    watch.line()  # Lebenszeichen: power.conf wurde inzwischen erneut gelesen
    watch.close()
    assert first.startswith("watch 3 vm=NOT_CONFIGURED name=- others=? names=- ")
    # Einmal je Sitzung, nicht bei jedem erneuten Lesen.
    assert guard.syslog().count("power.conf is missing or invalid") == 1


def test_watch_reports_an_unknown_uuid(guard: Guard) -> None:
    guard.set_vms((OTHER_UUID, "Second VM", "running"))
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert line.startswith(f"watch 3 vm=NOT_FOUND name=- others=1 names={hex_of('Second VM')} ")


def test_watch_sends_keepalives_without_changes(guard: Guard) -> None:
    guard.set_vms((UUID, "Gaming VM", "shut off"))
    watch = guard.start_watch()
    first = watch.line()
    started = time.monotonic()
    keepalive = watch.line(timeout=3)
    waited = time.monotonic() - started
    watch.close()
    assert keepalive == first
    assert 0.4 < waited < 2.0


def test_watch_sends_changes_at_once(tmp_path: Path) -> None:
    guard = make_guard(tmp_path, WATCH_KEEPALIVE_MS="20000")
    try:
        guard.set_vms((UUID, "Gaming VM", "shut off"))
        watch = guard.start_watch()
        assert " vm=SHUTOFF " in watch.line()
        guard.set_vms((UUID, "Gaming VM", "running"))
        started = time.monotonic()
        assert " vm=RUNNING " in watch.line(timeout=5)
        assert time.monotonic() - started < 3
        watch.close()
    finally:
        kill_loops(tmp_path)


def test_watch_rereads_power_conf(guard: Guard) -> None:
    guard.set_vms((UUID, "Gaming VM", "running"), (OTHER_UUID, "Second VM", "shut off"))
    watch = guard.start_watch()
    assert " vm=RUNNING " in watch.line()
    guard.configure(f"vm {OTHER_UUID}\n")
    line = watch.line_with("vm=SHUTOFF")
    watch.close()
    assert f" name={hex_of('Second VM')} others=1 names={hex_of('Gaming VM')} " in line


def test_watch_logs_once_per_session(guard: Guard) -> None:
    watch = guard.start_watch()
    for _ in range(3):
        watch.line()
    watch.close()
    assert guard.syslog() == ["vm-watch started"]


def test_watch_ends_when_twinraid_disconnects(guard: Guard) -> None:
    watch = guard.start_watch()
    watch.line()
    session = watch.process.pid
    watch.close()
    # Spätestens beim nächsten Lebenszeichen scheitert das Schreiben.
    watch.process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while processes_of(session) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert processes_of(session) == []


def test_at_most_two_watch_loops(guard: Guard) -> None:
    first, second = guard.start_watch(), guard.start_watch()
    first.line()
    second.line()
    third = guard.run("status", "vm-watch")
    assert third.returncode == 3
    assert third.stderr == "twinraid-guard: refused watch-busy\n"
    assert "refused: watch-busy" in guard.syslog()
    first.close()
    first.process.wait(timeout=5)
    # Auch Kindprozesse wie sleep halten die Sperre, bis sie enden.
    deadline = time.monotonic() + 3
    while processes_of(first.process.pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    fourth = guard.start_watch()
    fourth.line()
    second.close()
    fourth.close()


@pytest.mark.parametrize(
    "marker",
    [
        "boot/config/plugins/dynamix.unraid.net.plg",
        "boot/config/plugins/dynamix.unraid.net.staging.plg",
        "var/log/plugins/dynamix.unraid.net.plg",
        "var/log/plugins/dynamix.unraid.net.staging.plg",
    ],
)
def test_watch_reports_unraid_connect(guard: Guard, marker: str) -> None:
    (guard.root / marker).touch()
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert " connect=present flash_backup=inactive " in line


def test_other_plugins_are_no_connect_marker(guard: Guard) -> None:
    (guard.root / "boot/config/plugins/dynamix.my.servers").mkdir()
    (guard.root / "boot/config/plugins/other.plg").touch()
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert " connect=absent flash_backup=inactive " in line


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "inactive"),
        ('activated="true"\n', "active"),
        ("activated=1\n", "active"),
        ('[flashbackup]\nactivated = "2026-10-16"\r\n', "active"),
        ('activated=""\n', "inactive"),
        ('activated="false"\n', "inactive"),
        ("activated=0\n", "inactive"),
        ("activated=off\n", "inactive"),
        ('other="true"\n', "inactive"),
        ("", "inactive"),
        ("directory", "unknown"),
        ("unreadable", "unknown"),
    ],
)
def test_watch_reports_flash_backup(guard: Guard, content: str | None, expected: str) -> None:
    ini = guard.root / "flashbackup.ini"
    if content == "directory":
        ini.mkdir()
    elif content == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root can read every file")
        ini.write_text('activated="true"\n')
        ini.chmod(0)
    elif content is not None:
        ini.write_text(content)
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert f" flash_backup={expected} arc_max=- c_max=-" in line


def test_watch_notices_connect_changes(guard: Guard) -> None:
    watch = guard.start_watch()
    assert " connect=absent " in watch.line()
    (guard.root / "var/log/plugins/dynamix.unraid.net.plg").touch()
    watch.line_with("connect=present")
    watch.close()


@pytest.mark.parametrize(
    ("arc_max", "arcstats", "expected"),
    [
        (None, {}, "arc_max=- c_max=-"),
        ("0", {"c_max": 16 * GIB, "size": 3 * GIB}, f"arc_max=0 c_max={16 * GIB}"),
        (str(4 * GIB), {"c_max": 4 * GIB}, f"arc_max={4 * GIB} c_max={4 * GIB}"),
        ("garbage", {"c_max": GIB}, f"arc_max=? c_max={GIB}"),
        ("0", {"size": GIB}, "arc_max=0 c_max=?"),
    ],
)
def test_watch_reports_the_arc_limit(
    guard: Guard, arc_max: str | None, arcstats: dict[str, int], expected: str
) -> None:
    guard.zfs_module(arc_max, **arcstats)
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert line.endswith(f" flash_backup=inactive {expected}")


def test_watch_notices_arc_changes(guard: Guard) -> None:
    guard.zfs_module("0", c_max=16 * GIB)
    watch = guard.start_watch()
    assert " arc_max=0 " in watch.line()
    guard.zfs_module(str(2 * GIB), c_max=2 * GIB)
    watch.line_with(f"arc_max={2 * GIB} c_max={2 * GIB}")
    watch.close()


# --- Skript -----------------------------------------------------------------------------


def test_repository_script_uses_fixed_paths_and_version() -> None:
    text = GUARD.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert "eval" not in text
    assert "readonly VERSION=3\n" in text
    assert "readonly POWEROFF=/sbin/poweroff\n" in text
    assert "readonly VIRSH=/usr/bin/virsh\n" in text
    assert "readonly LOGGER=/usr/bin/logger\n" in text
    assert "readonly POWER_CONF=/boot/config/twinraid/power.conf\n" in text
    assert "export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n" in text
    # zfs, setpriv und findmnt über den festen PATH; ersetzt nur im Test.
    assert "readonly ZFS=zfs\n" in text
    assert "readonly SETPRIV=setpriv\n" in text
    assert "readonly FINDMNT=findmnt\n" in text
    assert "readonly MEMINFO=/proc/meminfo\n" in text
    assert "readonly ZFS_MODULE=/sys/module/zfs\n" in text
    assert 'readonly ARC_MAX_FILE="${ZFS_MODULE}/parameters/zfs_arc_max"\n' in text
    assert "readonly ARCSTATS=/proc/spl/kstat/zfs/arcstats\n" in text
    assert "readonly MEMORY_MARGIN=1073741824\n" in text
    assert "readonly RECEIVE_LOCK=/var/run/twinraid-guard-receive.lock\n" in text


def test_watch_line_format_is_documented() -> None:
    """Das Format der Zeile steht so auch im Konzept (Abschnitt 3.5)."""
    concept = (REPO_ROOT / "docs" / "KONZEPT.md").read_text()
    assert (
        "watch 3 vm=<Zustand> name=<Hex|-> others=<Anzahl|?> names=<Hex>,<Hex>…|- "
        "connect=<present|absent> flash_backup=<active|inactive|unknown> "
        "arc_max=<Byte|-|?> c_max=<Byte|-|?>"
    ) in concept
