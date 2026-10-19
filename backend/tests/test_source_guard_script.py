"""host-scripts/source-guard.sh: Wächter auf Unraid Klein (Konzept 3.6).

Die Tests laufen mit einer Kopie des Skripts, in der feste Pfade, Programme und Zeiten
ersetzt sind: docker, zfs, zpool, mount, findmnt und logger kommen aus
tests/support/fake_source_host.py, Unraid-Pfade zeigen in einen Testordner, der
Besitzer ist der eigene Benutzer statt root, und die Schleife läuft im Takt von
Zehntelsekunden. Den Wächter mit echtem Docker und echtem tmpfs prüft
docker/source-guard-test.sh in der CI.
"""

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT
from twinraid.integrations.source_guard import parse_status

SCRIPT = REPO_ROOT / "host-scripts" / "source-guard.sh"
EXAMPLE_CONF = REPO_ROOT / "host-scripts" / "source-guard.conf.example"
BACKEND = REPO_ROOT / "backend"
TMPFS_OPTIONS = "size=64k,mode=0700,nosuid,nodev,noexec"
DEFAULT_CONF = "allow twinraid-testcontainer\nallow stopped-one\ndataset twinraidtest\n"
_SHIM = """#!{python}
import sys
sys.path.insert(0, {backend!r})
from tests.support.fake_source_host import main
sys.exit(main({tool!r}, sys.argv[1:]))
"""
_TEST_TIMES = {
    "MINUTE": "1",
    "LOOP_SLEEP": "0.05",
    "STATUS_INTERVAL": "1",
    "DOCKER_INTERVAL": "1",
    "CONTAINER_INTERVAL": "1",
    "ZFS_INTERVAL": "1",
    "HOST_INTERVAL": "1",
    "DOCKER_TIMEOUT": "5",
    "STOP_EXTRA": "5",
    "REREAD_DELAY": "0.05",
    "RESTART_DELAY": "0.2",
    "RESTART_LIMIT": "3",
}

Status = dict[str, Any]


def container_id(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def order_line(seq: int | str, text: str, *, checksum: str | None = None) -> bytes:
    """Ein Auftrag wie von TwinRaid: Text, Prüfsumme, auf 255 Zeichen aufgefüllt, "\\n"."""
    payload = f"TRSG1 {seq} {text}"
    if checksum is None:
        checksum = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"{payload} {checksum}".ljust(255).encode() + b"\n"


@dataclass
class Host:
    tmp: Path
    script: Path
    state: Path
    base: Path
    conf: Path
    plugins: Path
    loaded_plugins: Path
    flash_ini: Path
    alias: Path
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list)

    @property
    def request(self) -> Path:
        return self.base / "in" / "request"

    @property
    def status_file(self) -> Path:
        return self.base / "state" / "status.json"

    @property
    def private(self) -> Path:
        return self.base / "private"

    def env(self) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "FAKE_HOST_STATE": str(self.state)}

    def command(self, *arguments: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
            ["/bin/bash", str(self.script), *arguments],
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def start_run(self, *, wait: bool = True) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(  # noqa: S603 - feste Argumente, nur im Test
            ["/bin/bash", str(self.script), "run"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.processes.append(process)
        if wait:
            self.wait_status(lambda status: status["started_at"] > 0)
        return process

    def stop_run(self, process: subprocess.Popen[bytes], *, requested: bool = False) -> None:
        if requested:
            (self.private / "stop.request").touch()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
        (self.private / "stop.request").unlink(missing_ok=True)

    def status(self) -> Status:
        raw = self.status_file.read_bytes()
        result: Status = json.loads(raw)
        # Jeder Status, den der Wächter hier schreibt, muss das Schema von TwinRaid erfüllen.
        parse_status(raw)
        return result

    def wait_status(self, predicate: Callable[[Status], bool], timeout: float = 8) -> Status:
        deadline = time.monotonic() + timeout
        last: Status | None = None
        while time.monotonic() < deadline:
            try:
                last = self.status()
            except FileNotFoundError, json.JSONDecodeError:
                last = None
            if last is not None and predicate(last):
                return last
            time.sleep(0.05)
        raise AssertionError(f"status did not match in time: {last}")

    def write_order(self, data: bytes) -> None:
        self.request.write_bytes(data)

    def send(self, text: str, *, seq: int | None = None) -> Status:
        """Auftrag mit der nächsten Nummer; wartet, bis der Wächter ihn ausgeführt hat."""
        number = self.status()["last_seq"] + 1 if seq is None else seq
        self.write_order(order_line(number, text))
        status = self.wait_status(
            lambda s: s["last"] is not None and s["last"]["seq"] == number, timeout=15
        )
        last: Status = status["last"]
        return last

    def configure(self, text: str | None) -> None:
        if text is None:
            self.conf.unlink(missing_ok=True)
        else:
            self.conf.write_text(text)

    def containers(self) -> list[dict[str, Any]]:
        path = self.state / "containers.json"
        result: list[dict[str, Any]] = json.loads(path.read_text())
        return result

    def set_containers(self, *containers: dict[str, Any]) -> None:
        (self.state / "containers.json").write_text(json.dumps(list(containers)))

    def change_container(self, name: str, **values: Any) -> None:  # noqa: ANN401
        containers = self.containers()
        for container in containers:
            if container["name"] == name:
                container.update(values)
        self.set_containers(*containers)

    def running(self, name: str) -> bool:
        for container in self.containers():
            if container["name"] == name:
                return bool(container["running"])
        raise AssertionError(name)

    def set_zfs(self, value: dict[str, Any] | None) -> None:
        path = self.state / "zfs.json"
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(value))

    def flag(self, name: str, on: bool = True) -> None:
        path = self.state / name
        if on:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    def calls(self, tool: str) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        return [entry[1:] for entry in entries if entry[0] == tool]

    def syslog(self) -> list[str]:
        return [call[-1] for call in self.calls("logger")]

    def docker_changes(self) -> list[list[str]]:
        return [call for call in self.calls("docker") if call[0] in ("stop", "start")]


def container(name: str, **values: Any) -> dict[str, Any]:  # noqa: ANN401
    entry: dict[str, Any] = {
        "id": container_id(name),
        "name": name,
        "running": True,
        "image": "busybox:1.37.0",
        "labels": {},
        "mounts": [],
    }
    entry.update(values)
    return entry


def twinraid_container(base: Path, **values: Any) -> dict[str, Any]:  # noqa: ANN401
    return container(
        "TwinRaid",
        image="ghcr.io/mantua1/twinraid:latest",
        labels={"org.opencontainers.image.source": "https://github.com/mantua1/twinraid"},
        mounts=[
            {"Source": "/mnt/user/appdata/twinraid/config", "Destination": "/config"},
            {"Source": "/mnt/user/appdata/twinraid/data", "Destination": "/data"},
            {"Source": f"{base}/in/request", "Destination": "/srv/worker/source-guard/request"},
            {"Source": f"{base}/state", "Destination": "/srv/worker/source-guard/state"},
        ],
        **values,
    )


def make_host(tmp_path: Path, **constants: str) -> Host:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("docker", "zfs", "zpool", "mount", "findmnt", "logger"):
        shim = bin_dir / tool
        shim.write_text(_SHIM.format(python=sys.executable, backend=str(BACKEND), tool=tool))
        shim.chmod(0o755)
    state = tmp_path / "fake"
    state.mkdir()
    root = tmp_path / "root"
    for directory in ("boot/config/twinraid", "boot/config/plugins", "var/log/plugins", "run"):
        (root / directory).mkdir(parents=True)
    base = root / "run" / "twinraid-source-guard"
    paths = {
        "CONF": str(root / "boot/config/twinraid/source-guard.conf"),
        "BASE_DIR": str(base),
        "BASE_DIR_ALIAS": str(root / "var" / "run" / "twinraid-source-guard"),
        "OWNER_UID": str(os.getuid()),
        "DOCKER": str(bin_dir / "docker"),
        "ZFS": str(bin_dir / "zfs"),
        "ZPOOL": str(bin_dir / "zpool"),
        "LOGGER": str(bin_dir / "logger"),
        "MOUNT": str(bin_dir / "mount"),
        "FINDMNT": str(bin_dir / "findmnt"),
        "PLUGIN_DIR": str(root / "boot/config/plugins"),
        "LOADED_PLUGIN_DIR": str(root / "var/log/plugins"),
        "FLASHBACKUP_INI": str(root / "flashbackup.ini"),
    }
    text = SCRIPT.read_text()
    for name, value in {**paths, **_TEST_TIMES, **constants}.items():
        prefix = f"readonly {name}="
        lines = [line for line in text.splitlines() if line.startswith(prefix)]
        assert len(lines) == 1, name
        text = text.replace(lines[0] + "\n", f"{prefix}{value}\n")
    script = tmp_path / "source-guard.sh"
    script.write_text(text)
    host = Host(
        tmp=tmp_path,
        script=script,
        state=state,
        base=base,
        conf=Path(paths["CONF"]),
        plugins=Path(paths["PLUGIN_DIR"]),
        loaded_plugins=Path(paths["LOADED_PLUGIN_DIR"]),
        flash_ini=Path(paths["FLASHBACKUP_INI"]),
        alias=Path(paths["BASE_DIR_ALIAS"]),
    )
    host.set_containers(
        container("twinraid-testcontainer"),
        container("stopped-one", running=False),
        container("other"),
        twinraid_container(base),
    )
    host.configure(DEFAULT_CONF)
    return host


def _kill_leftovers(tmp_path: Path) -> None:
    """Kein Wächter darf den Test überleben."""
    marker = str(tmp_path).encode()
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                cmdline = (proc / "cmdline").read_bytes()
            except OSError:
                continue
            if marker in cmdline:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(proc.name), signal.SIGKILL)


@pytest.fixture
def host(tmp_path: Path) -> Iterator[Host]:
    instance = make_host(tmp_path)
    yield instance
    _kill_leftovers(tmp_path)


@pytest.fixture
def running(host: Host) -> Host:
    host.start_run()
    host.wait_status(lambda s: s["error"] is None and s["allowed"][0]["state"] == "running")
    return host


# --- Ablage, tmpfs und Auftragsdatei ------------------------------------------------------


def test_start_mounts_tmpfs_and_creates_files(host: Host) -> None:
    host.start_run()
    assert host.calls("mount") == [
        ["-t", "tmpfs", "-o", TMPFS_OPTIONS, "tmpfs", str(host.base / "in")]
    ]
    assert host.request.is_file()
    assert host.request.stat().st_size == 0
    assert host.request.stat().st_mode & 0o777 == 0o600
    assert (host.base / "in").stat().st_mode & 0o777 == 0o700
    assert (host.base / "state").stat().st_mode & 0o777 == 0o755
    assert host.private.stat().st_mode & 0o777 == 0o700
    status = host.wait_status(lambda s: s["error"] is None)
    assert host.status_file.stat().st_mode & 0o777 == 0o644
    assert status["protocol"] == 1
    assert status["version"] == 1
    assert len(status["instance"]) == 16
    assert status["last_seq"] == 0
    assert status["last"] is None
    assert status["run"] is None


def test_status_lists_allowed_containers_and_limits(running: Host) -> None:
    status = running.wait_status(lambda s: s["allowed"][1]["state"] == "stopped")
    assert status["allowed"] == [
        {
            "name": "twinraid-testcontainer",
            "id": container_id("twinraid-testcontainer"),
            "state": "running",
        },
        {"name": "stopped-one", "id": container_id("stopped-one"), "state": "stopped"},
    ]
    assert status["limits"] == {
        "max_stop_minutes": 20,
        "heartbeat_timeout_seconds": 120,
        "stop_timeout_seconds": 60,
        "min_run_interval_minutes": 30,
        "max_deletions": 1000,
        "snapshot_keep_days": 7,
        "snapshot_reserve_percent": 15,
    }
    assert status["target"] == {"configured": False, "port": 22}


def test_status_is_written_atomically(running: Host) -> None:
    for _ in range(200):
        json.loads(running.status_file.read_text())
    leftovers = [p.name for p in (running.base / "state").iterdir() if p.name != "status.json"]
    assert leftovers == []


def test_restart_keeps_the_request_file(host: Host) -> None:
    process = host.start_run()
    inode = host.request.stat().st_ino
    host.stop_run(process)
    host.start_run()
    assert host.request.stat().st_ino == inode
    assert len(host.calls("mount")) == 1


def test_directory_instead_of_request_file_is_reported_not_covered(host: Host) -> None:
    """Docker legt ein Verzeichnis an, wenn TwinRaid vor dem Wächter startet."""
    (host.base / "in" / "request").mkdir(parents=True)
    host.start_run()
    status = host.wait_status(lambda s: s["error"] == "request_not_a_file")
    assert status["error_detail"] == "request_is_directory"
    assert host.calls("mount") == []
    assert any("request is a directory" in line for line in host.syslog())
    assert any("restart the guard, then start TwinRaid" in line for line in host.syslog())


def test_unexpected_content_in_in_dir_is_not_covered(host: Host) -> None:
    (host.base / "in").mkdir(parents=True)
    (host.base / "in" / "other").write_text("x")
    host.start_run()
    status = host.wait_status(lambda s: s["error"] == "misconfigured")
    assert status["error_detail"] == "in_unexpected_content"
    assert host.calls("mount") == []


def test_symlinked_in_dir_is_refused(host: Host, tmp_path: Path) -> None:
    host.base.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (host.base / "in").symlink_to(tmp_path / "elsewhere")
    host.start_run()
    host.wait_status(lambda s: s["error"] == "misconfigured" and s["error_detail"] == "in_unusable")
    assert host.calls("mount") == []


def test_failed_mount_is_reported(host: Host) -> None:
    host.flag("mount-fails")
    host.start_run()
    host.wait_status(
        lambda s: s["error"] == "misconfigured" and s["error_detail"] == "mount_failed"
    )


def test_no_orders_while_the_layout_is_broken(host: Host) -> None:
    (host.base / "in").mkdir(parents=True)
    (host.base / "in" / "request").mkdir()
    host.start_run()
    host.wait_status(lambda s: s["error"] == "request_not_a_file")
    time.sleep(0.5)
    assert host.status()["last_seq"] == 0


# --- Konfiguration ------------------------------------------------------------------------


def test_example_config_is_valid(host: Host) -> None:
    host.configure(EXAMPLE_CONF.read_text())
    result = host.command("check-config")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 Container freigegeben, 1 Datasets" in result.stdout


@pytest.mark.parametrize(
    ("text", "detail", "line"),
    [
        ("allow ok\nunknown_key 1\n", "unknown_key", 2),
        ("allow bad;name\n", "bad_value", 1),
        ("allow -leading\n", "bad_value", 1),
        ("allow a\nallow a\n", "duplicate", 2),
        ("allow a\ndeny a\n", "duplicate", 2),
        ("dataset system\n", "forbidden_dataset", 1),
        ("dataset Domains\n", "forbidden_dataset", 1),
        ("dataset a/b\n", "bad_value", 1),
        ("dataset ../x\n", "bad_value", 1),
        ("max_stop_minutes 4\n", "bad_value", 1),
        ("max_stop_minutes 121\n", "bad_value", 1),
        ("heartbeat_timeout_seconds 29\n", "bad_value", 1),
        ("stop_timeout_seconds 9\n", "bad_value", 1),
        ("stop_timeout_seconds 1e3\n", "bad_value", 1),
        ("min_run_interval_minutes 9\n", "bad_value", 1),
        ("min_run_interval_minutes 1441\n", "bad_value", 1),
        ("target_host 999.1.1.1\n", "bad_value", 1),
        ("target_host Host_Name\n", "bad_value", 1),
        ("target_port 0\n", "bad_value", 1),
        ("snapshot_keep_days 0\n", "bad_value", 1),
        ("snapshot_reserve_percent 51\n", "bad_value", 1),
        ("max_stop_minutes 20\nmax_stop_minutes 30\n", "duplicate", 2),
        ("allow a b\n", "syntax", 1),
        ("allow\n", "syntax", 1),
        ("".join(f"allow c{i}\n" for i in range(51)), "too_many", 51),
        ("".join(f"dataset d{i}\n" for i in range(21)), "too_many", 21),
    ],
)
def test_invalid_config(host: Host, text: str, detail: str, line: int) -> None:
    host.configure(text)
    result = host.command("check-config")
    assert result.returncode == 1
    assert f"{detail} (Zeile {line})" in result.stdout


@pytest.mark.parametrize(
    "text",
    [
        "allow a  # Kommentar\n",
        "# nur Kommentar\n\n   \n",
        "allow a\r\ndataset b\r\n",
        "target_host 192.168.10.11\ntarget_port 2222\n",
        "min_run_interval_minutes 10\n",
        "min_run_interval_minutes 1440\n",
        "target_host groß.example\n".replace("ß", "ss"),
        "",
    ],
)
def test_valid_config(host: Host, text: str) -> None:
    host.configure(text)
    result = host.command("check-config")
    assert result.returncode == 0, result.stdout


def test_missing_or_symlinked_config_is_invalid(host: Host, tmp_path: Path) -> None:
    host.configure(None)
    assert "missing_file" in host.command("check-config").stdout
    (tmp_path / "real.conf").write_text("allow a\n")
    host.conf.symlink_to(tmp_path / "real.conf")
    assert "missing_file" in host.command("check-config").stdout


@pytest.mark.parametrize(
    "values",
    [
        {"image": "ghcr.io/mantua1/twinraid:0.5.0"},
        {"image": "ghcr.io/mantua1/twinraid@sha256:" + "a" * 64},
        {"image": "twinraid"},
        {"labels": {"org.opencontainers.image.source": "https://github.com/mantua1/twinraid"}},
        {"mounts": [{"Source": "BASE/state", "Destination": "/x"}]},
        {"mounts": [{"Source": "ALIAS/in/request", "Destination": "/x"}]},
        {"mounts": [{"Source": "ALIAS", "Destination": "/x"}]},
    ],
)
def test_allow_must_not_contain_twinraid(host: Host, values: dict[str, Any]) -> None:
    if "mounts" in values:
        source = values["mounts"][0]["Source"]
        source = source.replace("BASE", str(host.base)).replace("ALIAS", str(host.alias))
        values = {"mounts": [{"Source": source, "Destination": "/x"}]}
    host.change_container("twinraid-testcontainer", **values)
    result = host.command("check-config")
    assert result.returncode == 1
    assert "allow enthält TwinRaid" in result.stdout


def test_running_guard_refuses_orders_when_allow_contains_twinraid(host: Host) -> None:
    host.change_container("twinraid-testcontainer", image="ghcr.io/mantua1/twinraid:latest")
    host.start_run()
    status = host.wait_status(lambda s: s["error"] == "config_invalid")
    assert status["error_detail"] == "allow_contains_twinraid"
    assert host.send("begin")["result"] == "config_invalid"
    assert host.docker_changes() == []


def test_invalid_config_allows_nothing(host: Host) -> None:
    host.configure("allow twinraid-testcontainer\nallow bad;\n")
    host.start_run()
    status = host.wait_status(lambda s: s["error"] == "config_invalid")
    assert status["error_detail"] == "bad_value"
    assert status["config_line"] == 2
    assert status["allowed"] == []
    assert host.send("begin")["result"] == "config_invalid"
    assert any("config invalid: bad_value in line 2" in line for line in host.syslog())


# --- Aufträge -----------------------------------------------------------------------------


def test_run_stops_and_restarts_an_allowed_container(running: Host) -> None:
    assert running.send("begin") == {
        "seq": 1,
        "order": "begin",
        "container": None,
        "result": "opened",
        "at": running.status()["last"]["at"],
    }
    run = running.status()["run"]
    # Fristen laufen nach der Laufzeit; als Uhrzeit umgerechnet höchstens 1 s ungenau.
    assert abs(run["max_deadline"] - run["opened_at"] - 20) <= 1  # 20 "Minuten" zu je 1 s
    assert abs(run["heartbeat_deadline"] - run["opened_at"] - 120) <= 1
    last = running.send("stop twinraid-testcontainer")
    assert last["result"] == "stopped"
    assert last["container"] == "twinraid-testcontainer"
    assert not running.running("twinraid-testcontainer")
    stopped = running.status()["run"]["stopped"]
    assert stopped == [
        {"name": "twinraid-testcontainer", "id": container_id("twinraid-testcontainer")}
    ]
    assert running.send("ping")["result"] == "extended"
    assert running.send("finish")["result"] == "finished"
    assert running.running("twinraid-testcontainer")
    status = running.status()
    assert status["run"] is None
    assert status["last_run"]["reason"] == "finish"
    assert status["last_run"]["containers"] == [
        {
            "name": "twinraid-testcontainer",
            "id": container_id("twinraid-testcontainer"),
            "result": "started",
        }
    ]
    cid = container_id("twinraid-testcontainer")
    assert running.docker_changes() == [["stop", "-t", "60", "--", cid], ["start", "--", cid]]
    assert "order 2: stop twinraid-testcontainer: stopped" in running.syslog()


def test_ping_moves_only_the_heartbeat(running: Host) -> None:
    running.send("begin")
    before = running.status()["run"]
    time.sleep(2.1)
    running.send("ping")
    after = running.status()["run"]
    assert after["heartbeat_deadline"] > before["heartbeat_deadline"]
    assert abs(after["max_deadline"] - before["max_deadline"]) <= 1


def test_containers_restart_in_reverse_order(host: Host) -> None:
    host.configure("allow twinraid-testcontainer\nallow other\n")
    host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    host.send("stop twinraid-testcontainer")
    host.send("stop other")
    host.send("finish")
    starts = [call[-1] for call in host.docker_changes() if call[0] == "start"]
    assert starts == [container_id("other"), container_id("twinraid-testcontainer")]


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ("stop twinraid-testcontainer", "no_run"),
        ("ping", "no_run"),
        ("finish", "no_run"),
    ],
)
def test_orders_without_run(running: Host, order: str, expected: str) -> None:
    assert running.send(order)["result"] == expected
    assert running.docker_changes() == []


def test_second_begin_is_refused(running: Host) -> None:
    running.send("begin")
    assert running.send("begin")["result"] == "run_open"


def test_next_run_only_after_the_pause(running: Host) -> None:
    """Sonst könnte ein übernommener TwinRaid Container fast dauerhaft unten halten."""
    running.send("begin")
    running.send("stop twinraid-testcontainer")
    running.send("finish")
    status = running.status()
    assert status["run_allowed_at"] is not None
    assert abs(status["run_allowed_at"] - status["last_run"]["closed_at"] - 30) <= 1
    assert running.send("begin")["result"] == "too_soon"
    assert running.status()["run"] is None
    assert running.docker_changes()[-1][0] == "start"


def test_pause_ends_after_the_interval(host: Host) -> None:
    host.configure(DEFAULT_CONF + "min_run_interval_minutes 10\n")  # 10 s im Test
    host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    host.send("finish")
    assert host.send("begin")["result"] == "too_soon"
    host.wait_status(lambda s: s["run_allowed_at"] is None, timeout=15)
    assert host.send("begin")["result"] == "opened"


def test_pause_survives_a_restart(host: Host) -> None:
    process = host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    host.send("finish")
    host.stop_run(process)
    host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    assert host.send("begin")["result"] == "too_soon"


def test_reset_run_interval_lifts_the_pause(host: Host) -> None:
    host.command("start")
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    host.send("finish")
    assert host.send("begin")["result"] == "too_soon"
    result = host.command("reset-run-interval")
    assert result.returncode == 0, result.stderr
    assert "aufgehoben" in result.stdout
    host.wait_status(lambda s: s["run_allowed_at"] is None)
    assert host.send("begin")["result"] == "opened"
    assert "pause between runs lifted by root on Klein" in host.syslog()
    host.command("stop")


def test_reset_run_interval_without_running_guard(host: Host) -> None:
    result = host.command("reset-run-interval")
    assert result.returncode == 0
    assert "beim nächsten Start" in result.stdout


@pytest.mark.parametrize(
    ("config", "name", "expected"),
    [
        (DEFAULT_CONF, "other", "not_allowed"),
        (DEFAULT_CONF, "TwinRaid", "not_allowed"),
        (DEFAULT_CONF + "deny other\n", "other", "not_allowed"),
        (DEFAULT_CONF + "allow missing-one\n", "missing-one", "not_found"),
        (DEFAULT_CONF, "stopped-one", "not_running"),
    ],
)
def test_stop_is_refused(host: Host, config: str, name: str, expected: str) -> None:
    host.configure(config)
    host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    assert host.send(f"stop {name}")["result"] == expected
    assert host.docker_changes() == []
    assert host.status()["run"]["stopped"] == []


def test_name_matching_an_id_prefix_is_not_found(host: Host) -> None:
    """Docker findet über einen ID-Präfix einen anderen Container: der gilt nicht."""
    prefix = container_id("other")[:12]
    host.configure(f"allow {prefix}\n")
    host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    assert host.send(f"stop {prefix}")["result"] == "not_found"
    assert host.running("other")
    assert host.docker_changes() == []


def test_container_that_turned_into_twinraid_is_never_stopped(running: Host) -> None:
    running.send("begin")
    running.change_container("twinraid-testcontainer", image="twinraid:local")
    assert running.send("stop twinraid-testcontainer")["result"] in (
        "denied_self",
        "config_invalid",
    )
    assert running.running("twinraid-testcontainer")
    assert running.docker_changes() == []


def test_failed_stop_is_reported(running: Host) -> None:
    running.change_container("twinraid-testcontainer", stop_fails=True)
    running.send("begin")
    assert running.send("stop twinraid-testcontainer")["result"] == "stop_failed"
    running.send("finish")
    assert running.status()["last_run"]["containers"][0]["result"] == "already_running"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("recreated", "changed"),
        ("removed", "gone"),
        ("started", "already_running"),
        ("start_fails", "start_failed"),
    ],
)
def test_finish_results(running: Host, change: str, expected: str) -> None:
    running.send("begin")
    running.send("stop twinraid-testcontainer")
    containers = [c for c in running.containers() if c["name"] != "twinraid-testcontainer"]
    original = next(c for c in running.containers() if c["name"] == "twinraid-testcontainer")
    if change == "recreated":
        running.set_containers(*containers, {**original, "id": "f" * 64, "running": False})
    elif change == "removed":
        running.set_containers(*containers)
    elif change == "started":
        running.change_container("twinraid-testcontainer", running=True)
    else:
        running.change_container("twinraid-testcontainer", start_fails=True)
    assert running.send("finish")["result"] == "finished"
    assert running.status()["last_run"]["containers"][0]["result"] == expected
    starts = [call for call in running.docker_changes() if call[0] == "start"]
    if change in ("recreated", "removed", "started"):
        assert starts == []


@pytest.mark.parametrize("order", ["snapshot", "prepare", "confirm", "send", "close"])
def test_zfs_orders_are_not_supported_yet(running: Host, order: str) -> None:
    assert running.send(order)["result"] == "not_supported"


# --- Format der Auftragsdatei -------------------------------------------------------------


def _assert_rejected(host: Host, data: bytes) -> None:
    before = host.status()
    host.write_order(data)
    status = host.wait_status(lambda s: s["rejected"]["count"] == before["rejected"]["count"] + 1)
    assert status["last_seq"] == before["last_seq"]
    assert status["last"] == before["last"]


@pytest.mark.parametrize(
    "data",
    [
        order_line(1, "begin", checksum="0" * 16),
        order_line(1, "stop a;reboot"),
        order_line(1, "stop $(reboot)"),
        order_line(1, "stop a b"),
        order_line(1, "stop " + "a" * 129),
        order_line(1, "Begin"),
        order_line(1, "rm"),
        order_line("01", "begin"),
        order_line("1000000000", "begin"),
        order_line(0, "begin"),
        order_line(1, "begin").replace(b"TRSG1", b"TRSG2"),
        order_line(1, "begin").replace(b"TRSG1", b"trsg1"),
        order_line(1, "begin")[:-1] + b" ",
        order_line(1, "begin")[:200],
        b"TRSG1 1 begin\n",
        order_line(1, "begin")[:100] + b"\x00" + order_line(1, "begin")[101:],
        order_line(1, "begin").replace(b"    \n", "äö \n".encode()[:5]),
        order_line(1, "begin") + b"x" * 44,
        b"\n" * 256,
    ],
)
def test_malformed_orders_are_rejected(running: Host, data: bytes) -> None:
    _assert_rejected(running, data)
    assert running.status()["run"] is None


def test_rejected_content_never_reaches_syslog_or_status(running: Host) -> None:
    marker = "leakmarker123"
    _assert_rejected(running, order_line(1, f"stop {marker}", checksum="1" * 16))
    _assert_rejected(running, order_line(1, f"stop {marker};x"))
    assert marker not in running.status_file.read_text()
    assert not any(marker in line for line in running.syslog())


def test_rejections_are_logged_at_most_once_a_minute(running: Host) -> None:
    for number in range(1, 4):
        _assert_rejected(running, order_line(number, "begin", checksum=f"{number:016d}"))
    assert running.syslog().count("order rejected (rejected_format)") == 1


def test_old_numbers_are_ignored(running: Host) -> None:
    running.send("begin")
    running.send("finish")
    running.write_order(order_line(2, "begin"))
    time.sleep(0.6)
    running.write_order(order_line(1, "begin"))
    time.sleep(0.6)
    status = running.status()
    assert status["last_seq"] == 2
    assert status["last"]["order"] == "finish"
    assert status["run"] is None


def test_gaps_in_numbers_are_allowed(running: Host) -> None:
    assert running.send("begin", seq=7)["result"] == "opened"
    assert running.status()["last_seq"] == 7


def test_same_number_after_a_bad_checksum_is_accepted(running: Host) -> None:
    _assert_rejected(running, order_line(1, "begin", checksum="2" * 16))
    assert running.send("begin", seq=1)["result"] == "opened"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_unusable_request_file_is_reported_and_recovers(running: Host, kind: str) -> None:
    other = running.tmp / "other"
    other.write_text("x")
    if kind == "symlink":
        running.request.unlink()
        running.request.symlink_to(other)
        detail = "request_is_symlink"
    else:
        os.link(running.request, running.base / "in" / "second")
        detail = "request_hardlink"
    status = running.wait_status(lambda s: s["error"] == "request_not_a_file")
    assert status["error_detail"] == detail
    if kind == "symlink":
        running.request.unlink()
    else:
        (running.base / "in" / "second").unlink()
    running.wait_status(lambda s: s["error"] is None)
    assert running.send("begin")["result"] == "opened"


def test_missing_request_file_is_created_again(running: Host) -> None:
    running.request.unlink()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any("restart TwinRaid" in line for line in running.syslog()):
            break
        time.sleep(0.05)
    assert any("restart TwinRaid" in line for line in running.syslog())
    assert running.request.is_file()


# --- Totmannschalter ----------------------------------------------------------------------


def test_deadman_max_restarts_the_containers(host: Host) -> None:
    host.configure(DEFAULT_CONF + "max_stop_minutes 5\n")  # 5 s im Test
    host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    host.send("stop twinraid-testcontainer")
    deadline = time.monotonic() + 10
    while host.status()["run"] is not None and time.monotonic() < deadline:
        host.send("ping")
        time.sleep(0.5)
    status = host.status()
    assert status["run"] is None
    assert status["last_run"]["reason"] == "deadman_max"
    assert host.running("twinraid-testcontainer")
    assert any("deadman" in line for line in host.syslog())
    # Auch nach dem Totmannschalter gilt die Pause bis zum nächsten Lauf.
    assert host.send("begin")["result"] == "too_soon"


def test_deadman_heartbeat_after_a_restart(host: Host) -> None:
    """Der Lauf übersteht einen Neustart des Wächters; die Frist gilt weiter."""
    process = host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    host.send("stop twinraid-testcontainer")
    host.stop_run(process)
    run_file = host.private / "run"
    lines = run_file.read_text().splitlines()
    past = _uptime() - 1
    run_file.write_text(
        "\n".join(f"heartbeat {past}" if line.startswith("heartbeat ") else line for line in lines)
        + "\n"
    )
    host.start_run()
    status = host.wait_status(lambda s: s["last_run"] is not None)
    assert status["last_run"]["reason"] == "deadman_heartbeat"
    assert host.running("twinraid-testcontainer")


def test_run_survives_a_restart_and_finish_starts_the_container(host: Host) -> None:
    process = host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    instance = host.status()["instance"]
    host.send("begin")
    host.send("stop twinraid-testcontainer")
    host.stop_run(process)
    assert not host.running("twinraid-testcontainer")
    host.start_run()
    status = host.wait_status(lambda s: s["error"] is None)
    assert status["instance"] == instance
    assert status["last_seq"] == 2
    assert status["run"]["stopped"][0]["name"] == "twinraid-testcontainer"
    assert host.send("finish")["result"] == "finished"
    assert host.running("twinraid-testcontainer")


def test_plain_sigterm_does_not_finish(running: Host) -> None:
    """Etwa beim Herunterfahren von Klein: kein finish, der Lauf bleibt gespeichert."""
    running.send("begin")
    running.send("stop twinraid-testcontainer")
    running.stop_run(running.processes[0])
    assert not running.running("twinraid-testcontainer")
    assert (running.private / "run").exists()


def test_requested_stop_finishes_the_run(running: Host) -> None:
    running.send("begin")
    running.send("stop twinraid-testcontainer")
    running.stop_run(running.processes[0], requested=True)
    assert running.running("twinraid-testcontainer")
    assert running.status()["last_run"]["reason"] == "stop_command"


# --- Neustarts, Auftragsnummer, verlorener Zustand ---------------------------------------


def test_fresh_run_directory_starts_a_new_instance(host: Host) -> None:
    process = host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    first = host.status()["instance"]
    host.send("begin")
    host.stop_run(process)
    _remove_tree(host.base)
    (host.state / "mounts.json").unlink()
    host.start_run()
    status = host.wait_status(lambda s: s["error"] is None)
    assert status["instance"] != first
    assert status["last_seq"] == 0
    assert status["run"] is None


def test_lost_state_discards_the_found_order(host: Host) -> None:
    process = host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    host.stop_run(process)
    host.write_order(order_line(7, "begin"))
    _remove_tree(host.private)
    host.start_run()
    status = host.wait_status(lambda s: s["error"] == "state_lost")
    assert status["last_seq"] == 7
    assert status["run"] is None
    assert any("discarded_at_start" in line for line in host.syslog())
    time.sleep(0.5)
    assert host.status()["run"] is None


def test_docker_unavailable_at_start_keeps_trying(host: Host) -> None:
    host.flag("docker-down")
    host.start_run()
    status = host.wait_status(lambda s: s["error"] == "docker_unavailable")
    first = status["heartbeat_at"]
    assert host.send("begin")["result"] == "docker_unavailable"
    host.wait_status(lambda s: s["heartbeat_at"] > first)
    host.flag("docker-down", on=False)
    host.wait_status(lambda s: s["error"] is None)
    assert host.send("begin")["result"] == "opened"
    assert "docker available" in host.syslog()


def test_finish_waits_for_docker(running: Host) -> None:
    running.send("begin")
    running.send("stop twinraid-testcontainer")
    running.flag("docker-down")
    assert running.send("finish")["result"] == "docker_unavailable"
    assert running.status()["run"] is not None
    running.flag("docker-down", on=False)
    assert running.send("finish")["result"] == "finished"
    assert running.running("twinraid-testcontainer")


# --- Überwachung, Start und Stopp ---------------------------------------------------------


def test_start_runs_one_supervisor_and_stop_ends_it(host: Host) -> None:
    result = host.command("start")
    assert result.returncode == 0
    assert "gestartet" in result.stdout
    host.wait_status(lambda s: s["error"] is None)
    assert "läuft bereits" in host.command("start").stdout
    assert "Der Wächter läuft (PID" in host.command("status").stdout
    result = host.command("stop")
    assert result.returncode == 0, result.stderr
    assert "gestoppt" in result.stdout
    assert "läuft nicht" in host.command("status").stdout


def test_supervisor_restarts_a_killed_worker(host: Host) -> None:
    host.command("start")
    status = host.wait_status(lambda s: s["error"] is None)
    time.sleep(1.1)  # started_at zählt in Sekunden
    _kill_worker(host)
    restarted = host.wait_status(lambda s: s["started_at"] > status["started_at"], timeout=10)
    assert restarted["instance"] == status["instance"]
    host.command("stop")


def test_killed_supervisor_does_not_keep_the_lock(host: Host) -> None:
    """Nach kill -9 der Überwachungsschleife endet der Wächter, und start klappt wieder."""
    host.command("start")
    status = host.wait_status(lambda s: s["error"] is None)
    supervisor = int((host.private / "supervisor.pid").read_text())
    os.kill(supervisor, signal.SIGKILL)
    _wait_for(lambda: _pids(host, "run") == [])
    assert any("supervisor is gone" in line for line in host.syslog())
    time.sleep(1.1)  # started_at zählt in Sekunden
    result = host.command("start")
    assert "gestartet" in result.stdout, result.stdout
    restarted = host.wait_status(lambda s: s["started_at"] > status["started_at"])
    assert restarted["instance"] == status["instance"]
    # Genau ein Wächter und eine Überwachungsschleife; Subshells eines Wächters (etwa für
    # $(…)) tragen dieselbe Befehlszeile und zählen nicht.
    _wait_for(lambda: len(_guards(host, "run")) == 1 and len(_guards(host, "supervise")) == 1)
    # Eine Sekunde lang nie zwei Wächter und nie eine zweite oder wartende Sperre auf
    # run.lock: Nur wer sie hält, nimmt Aufträge an.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        assert len(_guards(host, "run")) == 1
        assert _run_lock(host) == (1, 0)
        time.sleep(0.05)
    host.command("stop")


def test_second_worker_waits_for_the_lock(host: Host) -> None:
    """Zwei Wächter lesen nie gleichzeitig Aufträge."""
    first = host.start_run()
    host.wait_status(lambda s: s["error"] is None)
    second = host.start_run(wait=False)
    time.sleep(1)
    assert second.poll() is None  # wartet auf die Sperre
    host.stop_run(first)
    host.wait_status(lambda s: s["error"] is None)
    assert second.poll() is None
    assert host.send("begin")["result"] == "opened"


def test_stop_command_finishes_an_open_run(host: Host) -> None:
    host.command("start")
    host.wait_status(lambda s: s["error"] is None)
    host.send("begin")
    host.send("stop twinraid-testcontainer")
    result = host.command("stop")
    assert result.returncode == 0, result.stderr
    assert host.running("twinraid-testcontainer")
    assert host.status()["last_run"]["reason"] == "stop_command"


def test_selftest_stops_and_starts_an_allowed_container(host: Host) -> None:
    host.command("start")
    host.wait_status(lambda s: s["error"] is None)
    result = host.command("selftest", "twinraid-testcontainer", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: angehalten und wieder gestartet." in result.stdout
    assert host.running("twinraid-testcontainer")
    host.command("stop")


def test_second_selftest_needs_the_pause_or_a_reset(host: Host) -> None:
    host.command("start")
    host.wait_status(lambda s: s["error"] is None)
    assert host.command("selftest", "twinraid-testcontainer", timeout=60).returncode == 0
    result = host.command("selftest", "twinraid-testcontainer", timeout=60)
    assert result.returncode == 1
    assert "reset-run-interval" in result.stderr
    host.command("reset-run-interval")
    result = host.command("selftest", "twinraid-testcontainer", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    host.command("stop")


@pytest.mark.parametrize(
    ("name", "expected"), [("other", "not_allowed"), ("TwinRaid", "not_allowed")]
)
def test_selftest_with_refused_container(host: Host, name: str, expected: str) -> None:
    host.command("start")
    host.wait_status(lambda s: s["error"] is None)
    result = host.command("selftest", name, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"OK: abgelehnt ({expected}), Container unverändert." in result.stdout
    assert host.docker_changes() == []
    host.command("stop")


@pytest.mark.parametrize("arguments", [[], ["unknown"], ["selftest"], ["selftest", "a;b"]])
def test_usage_errors(host: Host, arguments: list[str]) -> None:
    result = host.command(*arguments)
    assert result.returncode != 0


def _uptime() -> int:
    return int(float(Path("/proc/uptime").read_text().split()[0]))


def _pids(host: Host, word: str) -> list[int]:
    marker = f"{host.script} {word}".encode()
    found = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
                state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError, IndexError:
                continue
            if marker in cmdline and state != "Z":
                found.append(int(proc.name))
    return found


def _guards(host: Host, word: str) -> list[int]:
    """Prozesse des Wächters ohne ihre Subshells (deren Elternprozess selbst einer ist)."""
    pids = _pids(host, word)
    guards = []
    for pid in pids:
        try:
            parent = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except OSError, IndexError, ValueError:
            continue
        if parent not in pids:
            guards.append(pid)
    return guards


def _run_lock(host: Host) -> tuple[int, int]:
    """Sperren auf private/run.lock aus /proc/locks: (gehaltene, wartende)."""
    inode = (host.private / "run.lock").stat().st_ino
    held = waiting = 0
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if "FLOCK" not in fields:
            continue
        device = fields[fields.index("FLOCK") + 4]
        if int(device.rsplit(":", 1)[1]) != inode:
            continue
        if "->" in fields:
            waiting += 1
        else:
            held += 1
    return held, waiting


def _wait_for(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.05)


def _kill_worker(host: Host) -> None:
    marker = f"{host.script} run".encode()
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            if marker in cmdline:
                os.kill(int(proc.name), signal.SIGKILL)
                return
    raise AssertionError("worker not found")


def _remove_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


# --- ZFS lesend und eigenes Dataset ------------------------------------------------------

_ZFS = {
    "pool": "mainpool",
    "used": 429000000000,
    "available": 491000000000,
    "datasets": {
        "mainpool/twinraidtest": {
            "type": "filesystem",
            "referenced": 12345,
            "compression": "lz4",
            "mountpoint": "/mnt/mainpool/twinraidtest",
        },
        "mainpool/appdata/twinraid": {
            "type": "filesystem",
            "referenced": 1,
            "compression": "on",
            "mountpoint": "/mnt/mainpool/appdata/twinraid",
        },
    },
}


def test_without_zfs_the_guard_is_still_active(running: Host) -> None:
    status = running.status()
    assert status["error"] is None
    assert status["zfs"] == {
        "state": "unavailable",
        "pool": None,
        "datasets": [
            {"name": "twinraidtest", "present": None, "referenced": None, "compression": None}
        ],
        "self_dataset": "unknown",
    }


def test_zfs_values_and_own_dataset(host: Host) -> None:
    host.configure(DEFAULT_CONF + "dataset missing\n")
    host.set_zfs(_ZFS)
    host.start_run()
    status = host.wait_status(lambda s: s["zfs"]["self_dataset"] == "found")
    assert status["zfs"]["state"] == "ok"
    assert status["zfs"]["pool"] == {"used": 429000000000, "available": 491000000000}
    assert status["zfs"]["datasets"] == [
        {"name": "twinraidtest", "present": True, "referenced": 12345, "compression": "lz4"},
        {"name": "missing", "present": False, "referenced": None, "compression": None},
    ]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("outside", "outside"),
        ("no_dataset", "missing"),
        ("no_container", "no_container"),
        ("direct_path", "found"),
    ],
)
def test_own_dataset_states(host: Host, change: str, expected: str) -> None:
    zfs = json.loads(json.dumps(_ZFS))
    others = list(json.loads((host.state / "containers.json").read_text()))
    twinraid = next(c for c in others if c["name"] == "TwinRaid")
    if change == "outside":
        twinraid["mounts"][1]["Source"] = "/mnt/user/appdata/elsewhere/data"
    elif change == "no_dataset":
        del zfs["datasets"]["mainpool/appdata/twinraid"]
    elif change == "no_container":
        others = [c for c in others if c["name"] != "TwinRaid"]
    else:
        twinraid["mounts"][0]["Source"] = "/mnt/mainpool/appdata/twinraid/config"
    host.set_containers(*others)
    host.set_zfs(zfs)
    host.start_run()
    host.wait_status(lambda s: s["zfs"]["self_dataset"] == expected)


def test_untrusted_zfs_output_never_reaches_the_status(host: Host) -> None:
    zfs = json.loads(json.dumps(_ZFS))
    zfs["datasets"]["mainpool/twinraidtest"]["compression"] = 'lz4","x":"y'
    zfs["raw_output"] = {"mainpool": '1"\t2'}
    host.set_zfs(zfs)
    host.start_run()
    status = host.wait_status(lambda s: s["zfs"]["state"] == "ok")
    assert status["zfs"]["pool"] is None
    assert status["zfs"]["datasets"][0]["compression"] is None
    assert '"x"' not in host.status_file.read_text()


# --- Unraid Connect -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        "plugins/dynamix.unraid.net.plg",
        "plugins/dynamix.unraid.net.staging.plg",
        "loaded/dynamix.unraid.net.plg",
    ],
)
def test_connect_markers(host: Host, marker: str) -> None:
    directory, name = marker.split("/")
    (host.plugins if directory == "plugins" else host.loaded_plugins).joinpath(name).touch()
    host.start_run()
    host.wait_status(lambda s: s["host"]["connect"] == "present")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('activated="yes"\n', "active"),
        ("activated=1\n", "active"),
        ('activated=""\n', "inactive"),
        ("activated=off\n", "inactive"),
        (None, "inactive"),
    ],
)
def test_flash_backup(host: Host, content: str | None, expected: str) -> None:
    if content is not None:
        host.flash_ini.write_text(content)
    host.start_run()
    host.wait_status(lambda s: s["host"]["flash_backup"] == expected)


def test_other_plugins_are_no_connect_marker(host: Host) -> None:
    (host.plugins / "dynamix.my.servers").mkdir()
    host.start_run()
    host.wait_status(lambda s: s["host"]["connect"] == "absent")


# --- Skript -------------------------------------------------------------------------------


def test_repository_script_uses_fixed_paths() -> None:
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert "eval" not in text
    for line in (
        "readonly CONF=/boot/config/twinraid/source-guard.conf\n",
        "readonly BASE_DIR=/run/twinraid-source-guard\n",
        "readonly OWNER_UID=0\n",
        "readonly DOCKER=/usr/bin/docker\n",
        "readonly POOL=mainpool\n",
        "readonly TMPFS_OPTIONS=size=64k,mode=0700,nosuid,nodev,noexec\n",
        "readonly SELF_IMAGE=ghcr.io/mantua1/twinraid\n",
        "readonly SELF_SOURCE=https://github.com/mantua1/twinraid\n",
        "export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n",
    ):
        assert line in text, line


def test_docker_is_called_only_with_fixed_arguments() -> None:
    """Jeder Aufruf von Docker steht so im Skript; Namen und IDs folgen immer nach "--"."""
    text = SCRIPT.read_text()
    calls = [
        line.strip()
        for line in text.splitlines()
        if "docker_call " in line or '"${DOCKER}"' in line
    ]
    for call in calls:
        if call.startswith(("docker_call() {", "timeout --kill-after=2", "#")):
            continue
        if " inspect " in call or " stop " in call or " start " in call:
            assert " -- " in call, call
