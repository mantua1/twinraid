"""host-scripts/guard.sh Version 2: Nur feste Anfragen sind erlaubt (Konzept 3.5 und 5.6).

Die Tests laufen mit einer Kopie des Skripts, in der feste Pfade und Zeiten
ersetzt sind: virsh ist das nachgebaute virsh aus docker/fake-target, poweroff
und logger schreiben ihre Aufrufe mit, Unraid-Pfade zeigen in einen Testordner,
und vm-watch läuft im Takt von Zehntelsekunden.
"""

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

GUARD = REPO_ROOT / "host-scripts" / "guard.sh"
FAKE_VIRSH = REPO_ROOT / "docker" / "fake-target" / "virsh"
UUID = "0b5c4e1a-2f3d-4c5b-8a9e-112233445566"
OTHER_UUID = "7c6b5a49-3827-4161-9504-f3e2d1c0b9a8"
# Platzhalter: schreibt Name und Argumente als JSON-Liste in die Aufrufdatei.
_RECORDER = """#!{python}
import json, sys
from pathlib import Path
with open({calls!r}, "a") as handle:
    handle.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
"""
# Startet das nachgebaute virsh mit dem Python der Tests.
_VIRSH_SHIM = """#!{python}
import runpy, sys
sys.argv[0] = "virsh"
runpy.run_path({fake!r}, run_name="__main__")
"""
# Zeiten im Test: vm-watch fragt alle 0,1 s ab, Lebenszeichen nach 0,6 s.
_TEST_TIMES = {
    "QUERY_TIMEOUT": "1",
    "START_TIMEOUT": "2",
    "FORCE_TIMEOUT": "2",
    "SHUTDOWN_TIMEOUT": "2",
    "WATCH_SLEEP": "0.1",
    "WATCH_KEEPALIVE_MS": "600",
    "WATCH_CONF_MS": "300",
    "WATCH_CONNECT_MS": "300",
}
_LINE_PATTERN = (
    r"watch 2 vm=[A-Z_]+ name=(-|[0-9a-f]+) others=(\?|\d+) names=(-|[0-9a-f,]+) "
    r"connect=(present|absent) flash_backup=(active|inactive|unknown)"
)


def hex_of(text: str) -> str:
    return text.encode().hex()


@dataclass
class Watch:
    """Eine laufende vm-watch-Schleife; liest Zeilen mit Zeitlimit."""

    process: subprocess.Popen[bytes]
    selector: selectors.DefaultSelector = field(default_factory=selectors.DefaultSelector)

    def __post_init__(self) -> None:
        assert self.process.stdout is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def line(self, timeout: float = 5.0) -> str:
        assert self.process.stdout is not None
        if not self.selector.select(timeout):
            raise AssertionError("vm-watch sent no line in time")
        raw = self.process.stdout.readline()
        if not raw:
            raise AssertionError("vm-watch ended")
        line = raw.decode("ascii").rstrip("\n")
        assert re.fullmatch(_LINE_PATTERN, line), line
        return line

    def line_with(self, text: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            line = self.line(max(0.1, deadline - time.monotonic()))
            if text in line:
                return line
            if time.monotonic() >= deadline:
                raise AssertionError(f"{text!r} not in {line!r}")

    def close(self) -> None:
        """Wie ein Abbruch der SSH-Verbindung: TwinRaid liest nicht mehr."""
        self.selector.close()
        assert self.process.stdout is not None
        self.process.stdout.close()


@dataclass
class Guard:
    script: Path
    state: Path
    calls_file: Path
    power_conf: Path
    root: Path

    def env(self) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "FAKE_VIRSH_STATE": str(self.state)}

    def run(
        self, mode: str | None, request: str | None, timeout: float = 15
    ) -> subprocess.CompletedProcess[str]:
        env = self.env()
        if request is not None:
            env["SSH_ORIGINAL_COMMAND"] = request
        arguments = ["/bin/bash", str(self.script)]
        if mode is not None:
            arguments.append(mode)
        return subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
            arguments, env=env, capture_output=True, text=True, check=False, timeout=timeout
        )

    def start_watch(self) -> Watch:
        env = {**self.env(), "SSH_ORIGINAL_COMMAND": "vm-watch"}
        process = subprocess.Popen(  # noqa: S603 - feste Argumente, nur im Test
            ["/bin/bash", str(self.script), "status"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return Watch(process)

    def set_vms(self, *vms: tuple[str, str, str]) -> None:
        """(UUID, Name, Zustand wie bei virsh)."""
        entries = [{"uuid": uuid, "name": name, "state": state} for uuid, name, state in vms]
        (self.state / "vms.json").write_text(json.dumps(entries))

    def vm_states(self) -> dict[str, str]:
        entries = json.loads((self.state / "vms.json").read_text())
        return {entry["uuid"]: entry["state"] for entry in entries}

    def flag(self, name: str, on: bool = True) -> None:
        path = self.state / name
        if on:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    def configure(self, text: str | None = f"vm {UUID}\n") -> None:
        if text is None:
            self.power_conf.unlink(missing_ok=True)
        else:
            self.power_conf.write_text(text)

    def calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def syslog(self) -> list[str]:
        return [call[-1] for call in self.calls() if call[0] == "logger"]

    def poweroffs(self) -> int:
        return sum(1 for call in self.calls() if call[0] == "poweroff")

    def virsh_calls(self) -> list[str]:
        log = self.state / "virsh.log"
        return log.read_text().splitlines() if log.exists() else []

    def virsh_actions(self) -> list[str]:
        """Aufrufe von virsh, die etwas verändern."""
        actions = ("start ", "shutdown ", "destroy ")
        return [call for call in self.virsh_calls() if call.startswith(actions)]


def make_guard(tmp_path: Path, **times: str) -> Guard:
    """Kopie von guard.sh mit Testpfaden; times ersetzt einzelne Zeiten aus _TEST_TIMES."""
    calls = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("poweroff", "logger"):
        fake = bin_dir / name
        fake.write_text(_RECORDER.format(python=sys.executable, calls=str(calls)))
        fake.chmod(0o755)
    virsh = bin_dir / "virsh"
    virsh.write_text(_VIRSH_SHIM.format(python=sys.executable, fake=str(FAKE_VIRSH)))
    virsh.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    for directory in ("boot/config/twinraid", "boot/config/plugins", "var/log/plugins", "run"):
        (root / directory).mkdir(parents=True)
    paths = {
        "POWEROFF": str(bin_dir / "poweroff"),
        "VIRSH": str(virsh),
        "LOGGER": str(bin_dir / "logger"),
        "POWER_CONF": str(root / "boot/config/twinraid/power.conf"),
        "PLUGIN_DIR": str(root / "boot/config/plugins"),
        "LOADED_PLUGIN_DIR": str(root / "var/log/plugins"),
        "FLASHBACKUP_INI": str(root / "flashbackup.ini"),
        "WATCH_LOCK_DIR": str(root / "run"),
    }
    text = GUARD.read_text()
    for name, value in {**paths, **_TEST_TIMES, **times}.items():
        prefix = f"readonly {name}="
        lines = [line for line in text.splitlines() if line.startswith(prefix)]
        assert len(lines) == 1, name
        text = text.replace(lines[0] + "\n", f"{prefix}{value}\n")
    script = tmp_path / "guard.sh"
    script.write_text(text)
    instance = Guard(script, state, calls, Path(paths["POWER_CONF"]), root)
    instance.set_vms((UUID, "Gaming VM", "shut off"), (OTHER_UUID, "Second VM", "shut off"))
    instance.configure()
    return instance


def _kill_loops(tmp_path: Path) -> None:
    """Keine Schleife darf den Test überleben."""
    marker = str(tmp_path).encode()
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                cmdline = (proc / "cmdline").read_bytes()
            except OSError:
                continue
            if marker in cmdline:
                os.kill(int(proc.name), signal.SIGKILL)


@pytest.fixture
def guard(tmp_path: Path) -> Iterator[Guard]:
    yield make_guard(tmp_path)
    _kill_loops(tmp_path)


def _processes_of(session: int) -> list[int]:
    """Prozesse einer Sitzung (setsid), z. B. guard.sh mit sleep und virsh."""
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError, IndexError:
            continue
        # Nach dem Namen: Zustand, PPID, PGID, SID, …
        if int(fields[3]) == session and fields[0] != "Z":
            found.append(int(proc.name))
    return found


# --- check -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["status", "power"])
def test_check_reports_version_and_does_nothing(guard: Guard, mode: str) -> None:
    result = guard.run(mode, "check")
    assert result.returncode == 0
    assert result.stdout == f"twinraid-guard 2 {mode} ok\n"
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


@pytest.mark.parametrize("mode", ["status", "power"])
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


@pytest.mark.parametrize("mode", ["sync", "", "Power", "Status", "power extra", None])
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
        f"watch 2 vm={expected} name={hex_of('Gaming VM')} others=0 names=- "
        "connect=absent flash_backup=inactive"
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
    assert line.startswith(f"watch 2 {expected} ")
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
    assert first.startswith("watch 2 vm=NOT_CONFIGURED name=- others=? names=- ")
    # Einmal je Sitzung, nicht bei jedem erneuten Lesen.
    assert guard.syslog().count("power.conf is missing or invalid") == 1


def test_watch_reports_an_unknown_uuid(guard: Guard) -> None:
    guard.set_vms((OTHER_UUID, "Second VM", "running"))
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert line.startswith(f"watch 2 vm=NOT_FOUND name=- others=1 names={hex_of('Second VM')} ")


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
        _kill_loops(tmp_path)


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
    while _processes_of(session) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _processes_of(session) == []


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
    while _processes_of(first.process.pid) and time.monotonic() < deadline:
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
    assert line.endswith(" connect=present flash_backup=inactive")


def test_other_plugins_are_no_connect_marker(guard: Guard) -> None:
    (guard.root / "boot/config/plugins/dynamix.my.servers").mkdir()
    (guard.root / "boot/config/plugins/other.plg").touch()
    watch = guard.start_watch()
    line = watch.line()
    watch.close()
    assert line.endswith(" connect=absent flash_backup=inactive")


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
    assert line.endswith(f" flash_backup={expected}")


def test_watch_notices_connect_changes(guard: Guard) -> None:
    watch = guard.start_watch()
    assert " connect=absent " in watch.line()
    (guard.root / "var/log/plugins/dynamix.unraid.net.plg").touch()
    watch.line_with("connect=present")
    watch.close()


# --- Skript -----------------------------------------------------------------------------


def test_repository_script_uses_fixed_paths_and_version() -> None:
    text = GUARD.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert "eval" not in text
    assert "readonly VERSION=2\n" in text
    assert "readonly POWEROFF=/sbin/poweroff\n" in text
    assert "readonly VIRSH=/usr/bin/virsh\n" in text
    assert "readonly LOGGER=/usr/bin/logger\n" in text
    assert "readonly POWER_CONF=/boot/config/twinraid/power.conf\n" in text
    assert "export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n" in text


def test_watch_line_format_is_documented() -> None:
    """Das Format der Zeile steht so auch im Konzept (Abschnitt 3.5)."""
    concept = (REPO_ROOT / "docs" / "KONZEPT.md").read_text()
    assert (
        "watch 2 vm=<Zustand> name=<Hex|-> others=<Anzahl|?> names=<Hex>,<Hex>…|- "
        "connect=<present|absent> flash_backup=<active|inactive|unknown>"
    ) in concept
