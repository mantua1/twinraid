"""Testaufbau für host-scripts/source-guard.sh (Wächter auf Unraid Klein, Konzept 3.6).

Eine Kopie des Skripts mit ersetzten festen Pfaden, Programmen und Zeiten: docker, zfs,
zpool, mount, findmnt, logger, ssh, ssh-keygen, ssh-keyscan und ip kommen aus
tests/support/fake_source_host.py, Unraid-Pfade zeigen in einen Testordner, der Besitzer
ist der eigene Benutzer statt root, und die Schleife läuft im Takt von Zehntelsekunden.
"""

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    # Ab Version 2; die Fristen des Jobs kürzt ein Test bei Bedarf selbst.
    "SNAPSHOT_GAP": "0",
    "CHECK_TIMEOUT": "5",
}
_TOOLS = (
    "docker",
    "zfs",
    "zpool",
    "mount",
    "findmnt",
    "logger",
    "ssh",
    "ssh-keygen",
    "ssh-keyscan",
    "ip",
)

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

    @property
    def sync_key(self) -> Path:
        return self.conf.parent / "ssh-sync"

    @property
    def known_hosts(self) -> Path:
        return self.conf.parent / "target-known-hosts"

    def env(self) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "FAKE_HOST_STATE": str(self.state)}

    def command(
        self, *arguments: str, timeout: float = 30, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
            ["/bin/bash", str(self.script), *arguments],
            env=self.env(),
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
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

    def zfs(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads((self.state / "zfs.json").read_text())
        return result

    def update_zfs(self, change: Callable[[dict[str, Any]], None]) -> None:
        zfs = self.zfs()
        change(zfs)
        self.set_zfs(zfs)

    def answer_ssh(self, **answers: Any) -> None:  # noqa: ANN401 - JSON-Antworten
        """Antworten der nachgebauten Gegenseite: check und receive (Name oder default)."""
        (self.state / "ssh.json").write_text(json.dumps(answers))

    def received(self) -> list[dict[str, Any]]:
        path = self.state / "received.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def save(self, name: str, value: Any) -> None:  # noqa: ANN401 - JSON-Zustand
        (self.state / name).write_text(json.dumps(value))


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
    for tool in _TOOLS:
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
        "SSH": str(bin_dir / "ssh"),
        "SSH_KEYGEN": str(bin_dir / "ssh-keygen"),
        "SSH_KEYSCAN": str(bin_dir / "ssh-keyscan"),
        "IP": str(bin_dir / "ip"),
        "SYNC_KEY": str(root / "boot/config/twinraid/ssh-sync"),
        "KNOWN_HOSTS": str(root / "boot/config/twinraid/target-known-hosts"),
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


def kill_leftovers(tmp_path: Path) -> None:
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
