"""Nachgebautes Unraid Groß für die Tests von host-scripts/guard.sh (Konzept 3.5 und 3.7).

Die Tests laufen mit einer Kopie des Skripts, in der feste Pfade und Zeiten ersetzt
sind: virsh, zfs und findmnt sind die Nachbauten aus docker/fake-target (dieselben wie
im SSH-Test), poweroff, logger und setpriv schreiben ihre Aufrufe mit (setpriv führt
den Befehl danach unverändert aus, ohne die UID zu wechseln), Unraid-Pfade zeigen in
einen Testordner, und vm-watch läuft im Takt von Zehntelsekunden.
"""

import fcntl
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.conftest import REPO_ROOT

GUARD = REPO_ROOT / "host-scripts" / "guard.sh"
FAKE_DIR = REPO_ROOT / "docker" / "fake-target"
UUID = "0b5c4e1a-2f3d-4c5b-8a9e-112233445566"
OTHER_UUID = "7c6b5a49-3827-4161-9504-f3e2d1c0b9a8"
POOL = "mainpool"
ROOT = "mainpool/twinraid-mirror"
MOUNT_ROOT = "/mnt/mainpool/twinraid-mirror"
GIB = 1024**3
# Die 28 Rechte aus Z1b (Konzept 3.7), so sortiert, wie zfs allow sie ausgibt.
RECV_PERMS = (
    "acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,"
    "mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,"
    "refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,"
    "userprop,xattr"
)
# Ausgabe von zfs allow wie print_fs_perms in zfs_main.c (OpenZFS 2.4).
ALLOW_HEADER = f"---- Permissions on {ROOT} ".ljust(70, "-")
ALLOW_LINES = [
    ALLOW_HEADER,
    "Local+Descendent permissions:",
    f"\tuser (unknown: 64000) {RECV_PERMS}",
]
# Feste Werte der Empfangszeile (Konzept 3.7), ohne mountpoint.
FIXED = {
    "readonly": "on",
    "exec": "off",
    "setuid": "off",
    "devices": "off",
    "primarycache": "metadata",
    "secondarycache": "all",
    "compression": "lz4",
    "atime": "off",
    "xattr": "sa",
    "acltype": "posix",
    "recordsize": "128K",
    "snapdir": "hidden",
    "sync": "standard",
    "dedup": "off",
    "copies": "1",
    "checksum": "on",
    "quota": "none",
    "refquota": "none",
    "reservation": "none",
    "refreservation": "none",
    "canmount": "on",
    "sharesmb": "off",
    "sharenfs": "off",
    "twinraid:mirror": "1",
}
# Platzhalter: schreibt Name und Argumente als JSON-Liste in die Aufrufdatei.
_RECORDER = """#!{python}
import json, sys
from pathlib import Path
with open({calls!r}, "a") as handle:
    handle.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
"""
# setpriv: schreibt mit und führt den Befehl nach "--" aus (im Test ohne Wechsel der UID).
_SETPRIV = """#!{python}
import json, os, sys
with open({calls!r}, "a") as handle:
    handle.write(json.dumps(["setpriv", *sys.argv[1:]]) + "\\n")
command = sys.argv[sys.argv.index("--") + 1 :]
os.execv(command[0], command)
"""
# Startet einen Nachbau aus docker/fake-target mit dem Python der Tests.
_SHIM = """#!{python}
import runpy, sys
sys.argv[0] = {name!r}
runpy.run_path({fake!r}, run_name="__main__")
"""
# Zeiten im Test: vm-watch fragt alle 0,1 s ab, Lebenszeichen nach 0,6 s.
TEST_TIMES = {
    "QUERY_TIMEOUT": "1",
    "START_TIMEOUT": "2",
    "FORCE_TIMEOUT": "2",
    "SHUTDOWN_TIMEOUT": "2",
    "WATCH_SLEEP": "0.1",
    "WATCH_KEEPALIVE_MS": "600",
    "WATCH_CONF_MS": "300",
    "WATCH_CONNECT_MS": "300",
    # Halteprozess der Sperre nach poweroff: kurz, damit kein Test ihn 15 Minuten zurücklässt.
    "POWEROFF_LOCK_SECONDS": "2",
}
LINE_PATTERN = (
    r"watch 3 vm=[A-Z_]+ name=(-|[0-9a-f]+) others=(\?|\d+) names=(-|[0-9a-f,]+) "
    r"connect=(present|absent) flash_backup=(active|inactive|unknown) "
    r"arc_max=(-|\?|\d{1,20}) c_max=(-|\?|\d{1,20})"
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
        assert re.fullmatch(LINE_PATTERN, line), line
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
        return {
            "PATH": "/usr/bin:/bin",
            "FAKE_VIRSH_STATE": str(self.state),
            "FAKE_ZFS_STATE": str(self.state),
        }

    def run(
        self,
        mode: str | None,
        request: str | None,
        timeout: float = 15,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self.env()
        if request is not None:
            env["SSH_ORIGINAL_COMMAND"] = request
        arguments = ["/bin/bash", str(self.script)]
        if mode is not None:
            arguments.append(mode)
        return subprocess.run(  # noqa: S603 - feste Argumente, nur im Test
            arguments,
            env=env,
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
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

    # --- virsh ------------------------------------------------------------------------

    def set_vms(self, *vms: tuple[str, str, str], memory_kib: int | None = None) -> None:
        """(UUID, Name, Zustand wie bei virsh); memory_kib gilt für die erste VM."""
        entries: list[dict[str, Any]] = [
            {"uuid": uuid, "name": name, "state": state} for uuid, name, state in vms
        ]
        if memory_kib is not None:
            entries[0]["memory_kib"] = memory_kib
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

    # --- Speicher und ARC -----------------------------------------------------------------

    def meminfo(self, available_kib: int | None) -> None:
        lines = ["MemTotal:       32000000 kB", "MemFree:         1000000 kB"]
        if available_kib is not None:
            lines.append(f"MemAvailable:   {available_kib} kB")
        lines.append("Buffers:          100000 kB")
        (self.root / "proc/meminfo").write_text("\n".join(lines) + "\n")

    def zfs_module(self, arc_max: str | None, **arcstats: int) -> None:
        """arc_max None: kein ZFS geladen. arcstats: size, c_min, c_max in Byte."""
        module = self.root / "sys/module/zfs"
        stats = self.root / "proc/spl/kstat/zfs/arcstats"
        if arc_max is None:
            if (module / "parameters/zfs_arc_max").exists():
                (module / "parameters/zfs_arc_max").unlink()
                (module / "parameters").rmdir()
                module.rmdir()
            stats.unlink(missing_ok=True)
            return
        (module / "parameters").mkdir(parents=True, exist_ok=True)
        (module / "parameters/zfs_arc_max").write_text(arc_max + "\n")
        stats.parent.mkdir(parents=True, exist_ok=True)
        lines = ["13 1 0x01 147 39984 4253567891 98765432101", "name type data"]
        lines += ["hits 4 1234", "misses 4 56"]
        lines += [f"{name} 4 {value}" for name, value in arcstats.items()]
        stats.write_text("\n".join(lines) + "\n")

    # --- zfs ------------------------------------------------------------------------------

    def zfs_data(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.state / "zfs.json").read_text())
        return data

    def zfs_save(self, data: dict[str, Any]) -> None:
        (self.state / "zfs.json").write_text(json.dumps(data, indent=1))

    def objects(self) -> dict[str, Any]:
        objects: dict[str, Any] = self.zfs_data()["objects"]
        return objects

    def prop(self, name: str, prop: str) -> list[str] | None:
        stored = self.objects()[name]["props"].get(prop)
        return list(stored) if stored is not None else None

    def set_prop(self, name: str, prop: str, value: str, source: str = "local") -> None:
        data = self.zfs_data()
        data["objects"][name]["props"][prop] = [value, source]
        self.zfs_save(data)

    def drop_prop(self, name: str, prop: str) -> None:
        data = self.zfs_data()
        data["objects"][name]["props"].pop(prop, None)
        self.zfs_save(data)

    def change(self, name: str, **fields: object) -> None:
        data = self.zfs_data()
        data["objects"][name].update(fields)
        self.zfs_save(data)

    def add_dataset(self, name: str, kind: str = "filesystem", **props: str) -> None:
        data = self.zfs_data()
        data["txg"] += 1
        data["objects"][name] = {
            "type": kind,
            "props": {prop: [value, "local"] for prop, value in props.items()},
            "mounted": False,
            "txg": data["txg"],
        }
        self.zfs_save(data)

    def remove(self, name: str) -> None:
        data = self.zfs_data()
        del data["objects"][name]
        self.zfs_save(data)

    def add_mirror(
        self,
        name: str,
        snapshots: Sequence[tuple[str, int, int | None]] = (),
        mounted: bool = True,
    ) -> str:
        """Spiegel wie nach einem Empfang durch guard.sh; (Name, GUID, twinraid:received)."""
        dataset = f"{ROOT}/{name}"
        props = {prop: [value, "local"] for prop, value in FIXED.items()}
        props["mountpoint"] = [f"{MOUNT_ROOT}/{name}", "local"]
        data = self.zfs_data()
        data["txg"] += 1
        data["objects"][dataset] = {
            "type": "filesystem",
            "props": props,
            "mounted": mounted,
            "txg": data["txg"],
        }
        for snapshot, guid, received in snapshots:
            data["txg"] += 1
            snap_props: dict[str, list[str]] = {
                "guid": [str(guid), "-"],
                "creation": [str(1_760_000_000 + guid), "-"],
            }
            if received is not None:
                snap_props["twinraid:received"] = [str(received), "local"]
            data["objects"][f"{dataset}@{snapshot}"] = {
                "type": "snapshot",
                "props": snap_props,
                "txg": data["txg"],
            }
        self.zfs_save(data)
        return dataset

    def snapshots(self, dataset: str) -> list[str]:
        objects = self.objects()
        found = [name for name in objects if name.startswith(dataset + "@")]
        return [name.split("@", 1)[1] for name in sorted(found, key=lambda n: objects[n]["txg"])]

    def allow(self, lines: list[str] | None, dataset: str = ROOT) -> None:
        data = self.zfs_data()
        if lines is None:
            data["allow"].pop(dataset, None)
        else:
            data["allow"][dataset] = lines
        self.zfs_save(data)

    def deny_rights(self, *properties: str) -> None:
        """Rechte, die dem Empfang fehlen: zfs receive übergeht dann diese -o."""
        data = self.zfs_data()
        data["denied"] = list(properties)
        self.zfs_save(data)

    def zfs_calls(self) -> list[list[str]]:
        log = self.state / "zfs.log"
        if not log.exists():
            return []
        return [json.loads(line)["args"] for line in log.read_text().splitlines()]

    def zfs_changes(self) -> list[list[str]]:
        """Aufrufe von zfs, die etwas verändern."""
        changing = {"set", "receive", "mount", "unmount", "destroy", "inherit", "allow"}
        return [
            call
            for call in self.zfs_calls()
            if call[0] in changing and not (call[0] == "allow" and len(call) == 3)
        ]

    def setpriv_calls(self) -> list[list[str]]:
        return [call[1:] for call in self.calls() if call[0] == "setpriv"]

    def mirror_conf(self, text: str | None) -> None:
        path = self.root / "boot/config/twinraid/mirror.conf"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text)

    def lock_file(self) -> Path:
        return self.root / "run/twinraid-guard-receive.lock"

    def wait_for_lock_release(self, timeout: float = 6.0) -> None:
        """Bis der Halteprozess nach poweroff die Sperre freigibt (POWEROFF_LOCK_SECONDS)."""
        deadline = time.monotonic() + timeout
        with self.lock_file().open() as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("the receive lock was never released") from None
                    time.sleep(0.1)


def stream(
    *snapshots: tuple[str, int],
    full: bool = True,
    from_guid: int | None = None,
    **extra: object,
) -> str:
    """Strom für das nachgebaute zfs receive: Snapshots als (Name, GUID)."""
    payload: dict[str, Any] = {
        "full": full,
        "snapshots": [
            {"name": name, "guid": guid, "creation": 1_760_000_000 + guid}
            for name, guid in snapshots
        ],
        **extra,
    }
    if from_guid is not None:
        payload["from_guid"] = from_guid
    return json.dumps(payload)


def _write_tool(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def make_guard(tmp_path: Path, **constants: str) -> Guard:
    """Kopie von guard.sh mit Testpfaden; constants ersetzt einzelne Werte (z. B. Zeiten)."""
    calls = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("poweroff", "logger"):
        _write_tool(bin_dir / name, _RECORDER.format(python=sys.executable, calls=str(calls)))
    _write_tool(bin_dir / "setpriv", _SETPRIV.format(python=sys.executable, calls=str(calls)))
    for name in ("virsh", "zfs", "findmnt"):
        fake = str(FAKE_DIR / name)
        _write_tool(bin_dir / name, _SHIM.format(python=sys.executable, name=name, fake=fake))
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    for directory in (
        "boot/config/twinraid",
        "boot/config/plugins",
        "var/log/plugins",
        "run",
        "etc",
        "proc",
    ):
        (root / directory).mkdir(parents=True)
    paths = {
        "POWEROFF": str(bin_dir / "poweroff"),
        "VIRSH": str(bin_dir / "virsh"),
        "LOGGER": str(bin_dir / "logger"),
        "ZFS": str(bin_dir / "zfs"),
        "SETPRIV": str(bin_dir / "setpriv"),
        "FINDMNT": str(bin_dir / "findmnt"),
        "POWER_CONF": str(root / "boot/config/twinraid/power.conf"),
        "MIRROR_CONF": str(root / "boot/config/twinraid/mirror.conf"),
        "OWNER_UID": str(os.getuid()),
        "PLUGIN_DIR": str(root / "boot/config/plugins"),
        "LOADED_PLUGIN_DIR": str(root / "var/log/plugins"),
        "FLASHBACKUP_INI": str(root / "flashbackup.ini"),
        "MEMINFO": str(root / "proc/meminfo"),
        "ZFS_MODULE": str(root / "sys/module/zfs"),
        "ARCSTATS": str(root / "proc/spl/kstat/zfs/arcstats"),
        "WATCH_LOCK_DIR": str(root / "run"),
        "RECEIVE_LOCK": str(root / "run/twinraid-guard-receive.lock"),
        "PASSWD": str(root / "etc/passwd"),
        "GROUP": str(root / "etc/group"),
    }
    text = GUARD.read_text()
    for name, value in {**paths, **TEST_TIMES, **constants}.items():
        prefix = f"readonly {name}="
        lines = [line for line in text.splitlines() if line.startswith(prefix)]
        assert len(lines) == 1, name
        text = text.replace(lines[0] + "\n", f"{prefix}{value}\n")
    script = tmp_path / "guard.sh"
    script.write_text(text)
    guard = Guard(script, state, calls, Path(paths["POWER_CONF"]), root)
    guard.set_vms((UUID, "Gaming VM", "shut off"), (OTHER_UUID, "Second VM", "shut off"))
    guard.configure()
    guard.meminfo(16 * 1024 * 1024)
    (root / "etc/passwd").write_text(
        "root:x:0:0:Console and webGui login account:/root:/bin/bash\n"
        "nobody:x:99:100:nobody:/:/bin/false\n"
    )
    (root / "etc/group").write_text("root:x:0:root\nusers:x:100:\n")
    guard.zfs_save(
        {
            "objects": {
                POOL: {
                    "type": "filesystem",
                    "props": {
                        "mountpoint": ["/mnt/mainpool", "local"],
                        "compression": ["on", "local"],
                        "used": [str(100 * GIB), "-"],
                        "available": [str(900 * GIB), "-"],
                    },
                    "mounted": True,
                    "txg": 1,
                },
                ROOT: {
                    "type": "filesystem",
                    "props": {"available": [str(900 * GIB), "-"]},
                    "mounted": True,
                    "txg": 2,
                },
            },
            "allow": {ROOT: list(ALLOW_LINES)},
            "denied": [],
            "txg": 10,
        }
    )
    guard.mirror_conf("mirror twinraidtest\n")
    return guard


def kill_loops(tmp_path: Path) -> None:
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


def processes_of(session: int) -> list[int]:
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
