"""Nachgebauter Host von Unraid Klein für die Tests von host-scripts/source-guard.sh.

Stellt docker, zfs, zpool, mount, findmnt und logger nach. Der Zustand liegt als JSON
im Ordner aus FAKE_HOST_STATE; jeder Aufruf wird in calls.jsonl mitgeschrieben.
docker kennt nur die festen --format-Vorlagen des Wächters und scheitert bei jeder
anderen, damit eine geänderte Vorlage im Test auffällt.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

# Feste Vorlagen aus source-guard.sh (wörtlich, wie die Shell sie übergibt).
FORMAT_ID = "{{.Id}} {{.Name}} {{.State.Running}}"
FORMAT_SELF = (
    '{{.Config.Image}}{{"\\n"}}'
    '{{with .Config.Labels}}{{index . "org.opencontainers.image.source"}}{{end}}{{"\\n"}}'
    '{{range .Mounts}}{{.Source}}{{"\\n"}}{{end}}'
)
FORMAT_MOUNTS = '{{range .Mounts}}{{.Destination}}{{"\\t"}}{{.Source}}{{"\\n"}}{{end}}'
FORMAT_VERSION = "{{.ServerVersion}}"

Container = dict[str, Any]


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _state_dir() -> Path:
    return Path(os.environ["FAKE_HOST_STATE"])


def _record(tool: str, argv: list[str]) -> None:
    with (_state_dir() / "calls.jsonl").open("a") as handle:
        handle.write(json.dumps([tool, *argv]) + "\n")


def _load(name: str, default: Any) -> Any:  # noqa: ANN401 - JSON-Zustand beliebiger Form
    path = _state_dir() / name
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _save(name: str, value: Any) -> None:  # noqa: ANN401 - JSON-Zustand beliebiger Form
    path = _state_dir() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def _flag(name: str) -> bool:
    return (_state_dir() / name).exists()


def _find(containers: list[Container], ref: str) -> Container | None:
    """Wie Docker: zuerst der genaue Name, dann die ID, dann ein eindeutiger ID-Präfix."""
    for container in containers:
        if container["name"] == ref:
            return container
    for container in containers:
        if container["id"] == ref:
            return container
    matches = [c for c in containers if len(ref) >= 1 and c["id"].startswith(ref)]
    return matches[0] if len(matches) == 1 else None


def _docker(argv: list[str]) -> int:
    if _flag("docker-down"):
        _err("Cannot connect to the Docker daemon")
        return 1
    containers: list[Container] = _load("containers.json", [])
    if argv == ["info", "--format", FORMAT_VERSION]:
        _out("29.0.0")
        return 0
    if argv == ["ps", "-a", "-q", "--no-trunc"]:
        for container in containers:
            _out(container["id"])
        return 0
    if len(argv) == 7 and argv[:4] == ["inspect", "--type", "container", "--format"]:
        template, separator, ref = argv[4], argv[5], argv[6]
        if separator != "--":
            _err("missing --")
            return 99
        found = _find(containers, ref)
        if found is None:
            _err(f"Error: No such container: {ref}")
            return 1
        return _inspect(template, found)
    if len(argv) == 5 and argv[0] == "stop" and argv[1] == "-t" and argv[3] == "--":
        return _set_running(containers, argv[4], running=False, fail_key="stop_fails")
    if len(argv) == 3 and argv[:2] == ["start", "--"]:
        return _set_running(containers, argv[2], running=True, fail_key="start_fails")
    _err(f"unexpected docker call: {argv}")
    return 99


def _inspect(template: str, container: Container) -> int:
    mounts: list[dict[str, str]] = container.get("mounts", [])
    if template == FORMAT_ID:
        running = "true" if container["running"] else "false"
        _out(f"{container['id']} /{container['name']} {running}")
    elif template == FORMAT_SELF:
        labels: dict[str, str] = container.get("labels", {})
        text = container.get("image", "busybox:1.37.0") + "\n"
        text += labels.get("org.opencontainers.image.source", "") if labels else ""
        text += "\n"
        text += "".join(mount["Source"] + "\n" for mount in mounts)
        _out(text)
    elif template == FORMAT_MOUNTS:
        _out("".join(f"{m['Destination']}\t{m['Source']}\n" for m in mounts))
    else:
        _err(f"unexpected format: {template}")
        return 99
    return 0


def _set_running(containers: list[Container], ref: str, *, running: bool, fail_key: str) -> int:
    found = _find(containers, ref)
    if found is None or found["id"] != ref:
        _err("Error: No such container (only full IDs expected)")
        return 1
    if found.get(fail_key):
        _err("Error: failed")
        return 1
    found["running"] = running
    _save("containers.json", containers)
    return 0


def _zpool(argv: list[str]) -> int:
    zfs = _load("zfs.json", None)
    if argv[:4] == ["list", "-H", "-o", "name"] and argv[4:5] == ["--"] and len(argv) == 6:
        if zfs is not None and zfs.get("pool") == argv[5]:
            _out(argv[5])
            return 0
        _err(f"cannot open '{argv[5]}': no such pool")
        return 1
    return 99


def _zfs(argv: list[str]) -> int:
    zfs = _load("zfs.json", None)
    if zfs is None or len(argv) < 6 or argv[0] != "list" or argv[-2] != "--":
        return 1 if zfs is None else 99
    name = argv[-1]
    options = argv[1:-2]
    if "raw_output" in zfs and name in zfs["raw_output"]:
        _out(zfs["raw_output"][name])
        return 0
    if options == ["-H", "-p", "-o", "used,available"] and name == zfs["pool"]:
        _out(f"{zfs['used']}\t{zfs['available']}")
        return 0
    dataset = zfs.get("datasets", {}).get(name)
    if dataset is None:
        _err(f"cannot open '{name}': dataset does not exist")
        return 1
    if options == ["-H", "-p", "-o", "name,type,referenced,compression"]:
        _out(f"{name}\t{dataset['type']}\t{dataset['referenced']}\t{dataset['compression']}")
        return 0
    if options == ["-H", "-o", "name,type,mountpoint"]:
        _out(f"{name}\t{dataset['type']}\t{dataset['mountpoint']}")
        return 0
    return 99


def _mount(argv: list[str]) -> int:
    if len(argv) != 6 or argv[:2] != ["-t", "tmpfs"] or argv[2] != "-o" or argv[4] != "tmpfs":
        return 99
    if _flag("mount-fails"):
        return 32
    mounts: dict[str, str] = _load("mounts.json", {})
    mounts[argv[5]] = argv[3]
    _save("mounts.json", mounts)
    return 0


def _findmnt(argv: list[str]) -> int:
    if len(argv) != 5 or argv[:4] != ["-n", "-o", "FSTYPE", "-M"]:
        return 99
    mounts: dict[str, str] = _load("mounts.json", {})
    if argv[4] in mounts:
        _out("tmpfs")
        return 0
    return 1


def main(tool: str, argv: list[str]) -> int:
    _record(tool, argv)
    if tool == "docker":
        return _docker(argv)
    if tool == "zpool":
        return _zpool(argv)
    if tool == "zfs":
        return _zfs(argv)
    if tool == "mount":
        return _mount(argv)
    if tool == "findmnt":
        return _findmnt(argv)
    if tool == "logger":
        return 0
    return 99
