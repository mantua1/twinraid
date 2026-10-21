"""Nachgebauter Host von Unraid Klein für die Tests von host-scripts/source-guard.sh.

Stellt docker, zfs, zpool, mount, findmnt, logger, ssh, ssh-keygen, ssh-keyscan und ip
nach. Der Zustand liegt als JSON im Ordner aus FAKE_HOST_STATE; jeder Aufruf wird in
calls.jsonl mitgeschrieben. docker kennt nur die festen --format-Vorlagen des Wächters,
zfs und ssh nur die festen Befehlszeilen; jede andere Form scheitert mit Code 99, damit
eine geänderte Befehlszeile im Test auffällt.

ZFS (zfs.json): Datasets, Snapshots mit GUID, Erzeugung, Transaktionsgruppe,
Eigenschaften, Holds und Klonen, Lesezeichen. "changes" an einem Dataset bestimmt, was
zfs diff für den nächsten Snapshot meldet, "delta" die Größe eines inkrementellen
Stroms. "fail" nennt Vorgänge, die scheitern sollen, "send_seconds" die Dauer einer
Übertragung (mit Fortschrittszeilen wie zfs send -v -P).
"""

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import random
import sys
import time
from collections.abc import Iterator
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
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Wächter und Kindprozess rufen zfs gleichzeitig auf; Änderungen nur unter Sperre."""
    with (_state_dir() / "zfs.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


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
    if argv[:1] == ["send"] and "-n" not in argv:
        return _zfs_send(argv)
    with _locked():
        zfs = _load("zfs.json", None)
        if zfs is None:
            _err("The ZFS modules are not loaded.")
            return 1
        rc = _zfs_locked(zfs, argv)
        if rc == 0 and argv[0] in _CHANGING:
            _save("zfs.json", zfs)
        return rc


_CHANGING = ("snapshot", "hold", "release", "bookmark", "destroy")
_SNAPSHOT_PROPERTIES = "twinraid:created-by,guid,creation,userrefs,createtxg"


def _failing(zfs: dict[str, Any], operation: str) -> bool:
    return operation in zfs.get("fail", [])


def _snapshots_of(zfs: dict[str, Any], dataset: str) -> list[tuple[str, dict[str, Any]]]:
    items = [
        (name, snap)
        for name, snap in zfs.get("snapshots", {}).items()
        if name.split("@", 1)[0] == dataset
    ]
    return sorted(items, key=lambda item: int(item[1]["createtxg"]))


def _zfs_locked(zfs: dict[str, Any], argv: list[str]) -> int:
    if len(argv) < 3 or "--" not in argv:
        _err(f"unexpected zfs call: {argv}")
        return 99
    split = argv.index("--")
    command, options, names = argv[0], argv[1:split], argv[split + 1 :]
    snapshots: dict[str, Any] = zfs.setdefault("snapshots", {})
    bookmarks: dict[str, Any] = zfs.setdefault("bookmarks", {})
    if command == "list" and len(names) == 1:
        return _zfs_list(zfs, options, names[0])
    if command == "get":
        return _zfs_get(zfs, options, names)
    if command == "holds" and options == ["-H"] and len(names) == 1:
        snap = snapshots.get(names[0])
        if snap is None:
            _err(f"cannot open '{names[0]}': dataset does not exist")
            return 1
        for tag in snap.get("holds", []):
            _out(f"{names[0]}\t{tag}\tMon Oct 19 12:00 2026")
        return 0
    if command in ("hold", "release") and options == [] and len(names) == 2:
        tag, name = names
        snap = snapshots.get(name)
        if snap is None or _failing(zfs, command):
            _err(f"cannot {command} snapshot '{name}'")
            return 1
        holds: list[str] = snap.setdefault("holds", [])
        if command == "hold":
            if tag in holds:
                _err(f"cannot hold snapshot '{name}': tag already exists on this dataset")
                return 1
            holds.append(tag)
        else:
            if tag not in holds:
                _err(f"cannot release hold from snapshot '{name}': no such tag on this dataset")
                return 1
            holds.remove(tag)
        return 0
    if command == "bookmark" and options == [] and len(names) == 2:
        source, target = names
        snap = snapshots.get(source)
        if snap is None or "#" not in target or target in bookmarks or _failing(zfs, "bookmark"):
            _err(f"cannot create bookmark '{target}'")
            return 1
        bookmarks[target] = {"guid": snap["guid"], "createtxg": snap["createtxg"]}
        return 0
    if command == "snapshot" and options == ["-o", "twinraid:created-by=source-guard"]:
        return _zfs_snapshot(zfs, names)
    if command == "diff" and options == ["-H"] and len(names) == 2:
        return _zfs_diff(zfs, names[0], names[1])
    if command == "send" and options[:6] == ["-n", "-v", "-P", "-L", "-c", "-e"]:
        return _zfs_estimate(zfs, options[6:], names)
    if command == "destroy" and options == [] and len(names) == 1:
        name = names[0]
        snap = snapshots.get(name)
        if "@" not in name or snap is None:
            _err(f"could not find any snapshots to destroy; check snapshot names: {name}")
            return 1
        if snap.get("holds") or snap.get("clones") or _failing(zfs, "destroy"):
            _err(f"cannot destroy snapshot {name}: dataset is busy")
            return 1
        del snapshots[name]
        return 0
    _err(f"unexpected zfs call: {argv}")
    return 99


def _zfs_list(zfs: dict[str, Any], options: list[str], name: str) -> int:
    if "raw_output" in zfs and name in zfs["raw_output"]:
        _out(zfs["raw_output"][name])
        return 0
    if options == ["-H", "-p", "-o", "used,available"] and name == zfs["pool"]:
        _out(f"{zfs['used']}\t{zfs['available']}")
        return 0
    if options == [
        "-H",
        "-p",
        "-d",
        "1",
        "-t",
        "bookmark",
        "-o",
        "name,guid,createtxg",
        "-s",
        "createtxg",
    ]:
        if name not in zfs.get("datasets", {}):
            _err(f"cannot open '{name}': dataset does not exist")
            return 1
        marks = [(n, b) for n, b in zfs.get("bookmarks", {}).items() if n.split("#")[0] == name]
        for mark, values in sorted(marks, key=lambda item: int(item[1]["createtxg"])):
            _out(f"{mark}\t{values['guid']}\t{values['createtxg']}")
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
    if options == ["-H", "-o", "name,type"]:
        _out(f"{name}\t{dataset['type']}")
        return 0
    _err(f"unexpected zfs list: {options}")
    return 99


def _zfs_get(zfs: dict[str, Any], options: list[str], names: list[str]) -> int:
    snapshots: dict[str, Any] = zfs.get("snapshots", {})
    if options == ["-H", "-p", "-r", "-t", "snapshot", "-o", "name,property,value,source"]:
        if names[:1] != [_SNAPSHOT_PROPERTIES] or len(names) != 2:
            _err(f"unexpected properties: {names}")
            return 99
        if names[1] not in zfs.get("datasets", {}) or _failing(zfs, "get_snapshots"):
            _err(f"cannot open '{names[1]}': dataset does not exist")
            return 1
        for name, snap in _snapshots_of(zfs, names[1]):
            value, source = snap.get("props", {}).get("twinraid:created-by", ["-", "-"])
            _out(f"{name}\ttwinraid:created-by\t{value}\t{source}")
            _out(f"{name}\tguid\t{snap['guid']}\t-")
            _out(f"{name}\tcreation\t{snap['creation']}\t-")
            _out(f"{name}\tuserrefs\t{len(snap.get('holds', []))}\t-")
            _out(f"{name}\tcreatetxg\t{snap['createtxg']}\t-")
        return 0
    if len(names) == 2 and names[1] in snapshots:
        snap = snapshots[names[1]]
        if options == ["-H", "-p", "-o", "value"] and names[0] == "referenced":
            if _failing(zfs, "referenced"):
                return 1
            _out(str(snap["referenced"]))
            return 0
        if options == ["-H", "-o", "value"] and names[0] == "clones":
            _out(",".join(snap.get("clones", [])) or zfs.get("no_clones", ""))
            return 0
    if len(names) == 2 and options[:3] == ["-H", "-p", "-o"]:
        _err(f"cannot open '{names[1]}': dataset does not exist")
        return 1
    _err(f"unexpected zfs get: {options} {names}")
    return 99


def _zfs_snapshot(zfs: dict[str, Any], names: list[str]) -> int:
    snapshots: dict[str, Any] = zfs["snapshots"]
    datasets: dict[str, Any] = zfs.get("datasets", {})
    pools = {name.split("/")[0] for name in names}
    if _failing(zfs, "snapshot") or len(pools) != 1 or not names:
        _err("cannot create snapshots: permission denied")
        return 1
    for name in names:
        dataset = name.split("@", 1)[0]
        if "@" not in name or dataset not in datasets:
            _err(f"cannot open '{dataset}': dataset does not exist")
            return 1
        if name in snapshots:
            _err(f"cannot create snapshot '{name}': dataset already exists")
            return 1
    zfs["txg"] = int(zfs.get("txg", 1000)) + 1
    for name in names:
        dataset = datasets[name.split("@", 1)[0]]
        snapshots[name] = {
            "guid": str(random.randrange(10**17, 10**19)),  # noqa: S311 - nur im Test
            "creation": int(time.time()),
            "createtxg": zfs["txg"],
            "props": {"twinraid:created-by": ["source-guard", "local"]},
            "holds": [],
            "clones": [],
            "referenced": dataset["referenced"],
            "changes": dataset.get("changes", {}),
            "delta": dataset.get("delta", 1000),
        }
    return 0


def _base_exists(zfs: dict[str, Any], base: str) -> bool:
    return base in zfs.get("snapshots", {}) or base in zfs.get("bookmarks", {})


def _zfs_diff(zfs: dict[str, Any], base: str, new: str) -> int:
    snapshots: dict[str, Any] = zfs.get("snapshots", {})
    if base not in snapshots or new not in snapshots or _failing(zfs, "diff"):
        _err(f"Unable to obtain diffs: {base}")
        return 1
    mountpoint = "/mnt/" + new.split("@", 1)[0]
    for kind, count in snapshots[new].get("changes", {}).items():
        for index in range(int(count)):
            if kind == "R":
                _out(f"R\t{mountpoint}/old{index}\t{mountpoint}/new{index}")
            else:
                _out(f"{kind}\t{mountpoint}/file{index}")
    return 0


def _stream_size(zfs: dict[str, Any], increment: list[str], new: str) -> int | None:
    snap = zfs.get("snapshots", {}).get(new)
    if snap is None:
        return None
    if not increment:
        return int(snap["referenced"])
    if increment[0] != "-i" or len(increment) != 2 or not _base_exists(zfs, increment[1]):
        return None
    return int(snap["delta"])


def _zfs_estimate(zfs: dict[str, Any], increment: list[str], names: list[str]) -> int:
    if len(names) != 1 or _failing(zfs, "estimate"):
        return 1
    size = _stream_size(zfs, increment, names[0])
    if size is None:
        _err("cannot send: snapshot or base missing")
        return 1
    if increment:
        _out(f"incremental\t{increment[1]}\t{names[0]}\t{size}")
    else:
        _out(f"full\t{names[0]}\t{size}")
    _out(f"size\t{size}")
    return 0


def _zfs_send(argv: list[str]) -> int:
    """zfs send -L -c -e -v -P [-i <Basis>] -- <neu>: Strom auf stdout, Fortschritt auf stderr."""
    if argv[:6] != ["send", "-L", "-c", "-e", "-v", "-P"] or argv[-2] != "--":
        _err(f"unexpected zfs send: {argv}")
        return 99
    increment, new = argv[6:-2], argv[-1]
    with _locked():
        zfs = _load("zfs.json", {})
        size = _stream_size(zfs, increment, new)
        failing = _failing(zfs, "send")
        seconds = float(zfs.get("send_seconds", 0))
        guid = zfs.get("snapshots", {}).get(new, {}).get("guid", "-")
    if size is None:
        _err(f"cannot send '{new}': snapshot or base missing")
        return 1
    base = increment[1] if increment else "-"
    kind = f"incremental\t{base}\t{new}" if increment else f"full\t{new}"
    _err(f"{kind}\t{size}")
    _err(f"size\t{size}")
    sys.stderr.flush()
    sys.stdout.buffer.write(f"FAKESTREAM {new} {base} {guid} {size}\n".encode())
    sys.stdout.buffer.flush()
    steps = max(1, int(seconds / 0.2))
    for step in range(1, steps + 1):
        time.sleep(seconds / steps)
        sent = size * step // steps
        if failing and step * 2 > steps:
            _err(f"warning: cannot send '{new}': Input/output error")
            return 1
        _err(time.strftime("%H:%M:%S") + f"\t{sent}\t{new}")
        sys.stderr.flush()
    sys.stdout.buffer.write(b"x" * min(size, 4096))
    sys.stdout.buffer.flush()
    if failing:
        return 1
    return 0


# --- ssh, ssh-keygen, ssh-keyscan, ip -----------------------------------------------------

HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTwinRaidTestsOnly0000000000"


def _ssh(argv: list[str]) -> int:
    """Nur die Form der festen Argumentliste; die genaue Liste prüft der Test."""
    if len(argv) != 29 or argv[:3] != ["-F", "/dev/null", "-i"] or argv[26] != "--":
        _err(f"unexpected ssh call: {argv}")
        return 99
    key = Path(argv[3])
    if not key.is_file() or key.stat().st_mode & 0o077:
        _err("Load key: bad permissions")
        return 255
    answers: dict[str, Any] = _load("ssh.json", {})
    request = argv[28]
    if request == "check":
        answer = answers.get("check", {"rc": 255, "stderr": "ssh: connect: Connection refused"})
        time.sleep(float(answer.get("sleep", 0)))
        sys.stdout.write(answer.get("stdout", ""))
        sys.stderr.write(answer.get("stderr", ""))
        return int(answer.get("rc", 0))
    if request.startswith("receive "):
        name = request.split(" ", 1)[1]
        receive = answers.get("receive", {})
        answer = receive.get(name, receive.get("default", {"rc": 0}))
        data = sys.stdin.buffer.read()
        header = data.split(b"\n", 1)[0].decode(errors="replace")
        with (_state_dir() / "received.jsonl").open("a") as handle:
            handle.write(json.dumps({"name": name, "header": header, "bytes": len(data)}) + "\n")
        time.sleep(float(answer.get("sleep", 0)))
        sys.stderr.write(answer.get("stderr", ""))
        return int(answer.get("rc", 0))
    _err("twinraid-guard: denied")
    return 2


def _fingerprint(blob: str) -> str:
    digest = hashlib.sha256(base64.b64decode(blob + "=" * (-len(blob) % 4))).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _ssh_keygen(argv: list[str]) -> int:
    if (
        argv[:8] == ["-q", "-t", "ed25519", "-N", "", "-C", "twinraid-sync", "-f"]
        and len(argv) == 9
    ):
        if _flag("keygen-fails"):
            return 1
        path = Path(argv[8])
        blob = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + os.urandom(36)).decode()
        path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
        path.chmod(0o600)
        Path(f"{path}.pub").write_text(f"ssh-ed25519 {blob} twinraid-sync\n")
        return 0
    if argv[:2] == ["-l", "-f"] and len(argv) == 3:
        fields = Path(argv[2]).read_text().split()
        _out(f"256 {_fingerprint(fields[2])} {fields[0]} (ED25519)")
        return 0
    _err(f"unexpected ssh-keygen call: {argv}")
    return 99


def _ssh_keyscan(argv: list[str]) -> int:
    if len(argv) != 6 or argv[:3] != ["-t", "ed25519", "-p"] or argv[4] != "--":
        _err(f"unexpected ssh-keyscan call: {argv}")
        return 99
    answer = _load("keyscan.json", None)
    if answer is not None:
        _out(answer)
        return 0
    port, host = argv[3], argv[5]
    name = host if port == "22" else f"[{host}]:{port}"
    _out(f"{name} ssh-ed25519 {HOST_KEY}")
    return 0


def _ip(argv: list[str]) -> int:
    if len(argv) != 4 or argv[:3] != ["-4", "route", "get"]:
        _err(f"unexpected ip call: {argv}")
        return 99
    source = _load("ip.json", "192.168.10.10")
    if source is None:
        return 2
    _out(f"{argv[3]} dev br0 src {source} uid 0 \n    cache ")
    return 0


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
    if tool == "ssh":
        return _ssh(argv)
    if tool == "ssh-keygen":
        return _ssh_keygen(argv)
    if tool == "ssh-keyscan":
        return _ssh_keyscan(argv)
    if tool == "ip":
        return _ip(argv)
    return 99
