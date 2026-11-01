"""Status des Wächters (Protokoll 2) und Einbindungen für Tests (Konzept 3.6).

STATUS entspricht dem, was write_status in source-guard.sh schreibt: Lauf offen, letzter
Lauf, ZFS in Ordnung, letztes check von Groß, ein vorbereiteter Job.
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from twinraid.integrations.source_guard import GuardPaths

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
HEARTBEAT = int(NOW.timestamp()) - 4
OWN_ID = "a" * 64
OTHER_ID = "b" * 64
GUID = "1234567890123456789"
SNAPSHOT = "twinraid-20261019T115900Z"

# Wie write_status in source-guard.sh (Protokoll 2): Lauf offen, letzter Lauf, ZFS in
# Ordnung, letztes check von Groß, ein vorbereiteter Job.
STATUS: dict[str, Any] = {
    "protocol": 2,
    "version": 2,
    "heartbeat_at": HEARTBEAT,
    "started_at": HEARTBEAT - 3600,
    "instance": "0123456789abcdef",
    "error": None,
    "error_detail": None,
    "config_line": None,
    "last_seq": 7,
    "last": {
        "seq": 7,
        "order": "stop",
        "container": "twinraid-testcontainer",
        "result": "stopped",
        "at": HEARTBEAT - 30,
    },
    "rejected": {"count": 2, "last_at": HEARTBEAT - 600},
    "host": {"connect": "absent", "flash_backup": "inactive"},
    "limits": {
        "max_stop_minutes": 20,
        "heartbeat_timeout_seconds": 120,
        "stop_timeout_seconds": 60,
        "min_run_interval_minutes": 30,
        "max_deletions": 1000,
        "snapshot_keep_days": 7,
        "snapshot_reserve_percent": 15,
    },
    "target": {
        "configured": True,
        "port": 22,
        "key": True,
        "known_host": True,
        "check": {
            "at": HEARTBEAT - 20,
            "result": "ok",
            "summary": {
                "root": "ok",
                "conf": "ok",
                "deleg": "ok",
                "receive": "idle",
                "clones": 0,
                "available": 10**12,
                "size": 2 * 10**12,
                "reserve_percent": 15,
                "keep_days": 30,
                "min_interval_minutes": 10,
            },
            "mirrors": [
                {
                    "name": "twinraidtest",
                    "state": "mirror",
                    "props": "ok",
                    "count": 3,
                    "newest_guid": GUID,
                    "newest_created": HEARTBEAT - 90000,
                    "newest_received": HEARTBEAT - 89000,
                    "oldest_created": HEARTBEAT - 900000,
                },
                {
                    "name": "dokumente",
                    "state": "absent",
                    "props": None,
                    "count": None,
                    "newest_guid": None,
                    "newest_created": None,
                    "newest_received": None,
                    "oldest_created": None,
                },
            ],
        },
    },
    "allowed": [
        {"name": "twinraid-testcontainer", "id": OTHER_ID, "state": "stopped"},
        {"name": "stopped-one", "id": None, "state": "missing"},
    ],
    "run": {
        "opened_at": HEARTBEAT - 60,
        "heartbeat_deadline": HEARTBEAT + 60,
        "max_deadline": HEARTBEAT + 1140,
        "stopped": [{"name": "twinraid-testcontainer", "id": OTHER_ID}],
    },
    "run_allowed_at": None,
    "last_run": {
        "opened_at": HEARTBEAT - 7200,
        "closed_at": HEARTBEAT - 7000,
        "reason": "deadman_heartbeat",
        "containers": [{"name": "twinraid-testcontainer", "id": OTHER_ID, "result": "started"}],
    },
    "zfs": {
        "state": "ok",
        "pool": {"used": 100, "available": 900},
        "datasets": [
            {
                "name": "twinraidtest",
                "present": True,
                "referenced": 4096,
                "compression": "on",
                "snapshots": 4,
                "oldest_snapshot_at": HEARTBEAT - 500000,
                "base": {"guid": GUID, "created_at": HEARTBEAT - 90000},
                "bookmark": True,
            },
            {
                "name": "dokumente",
                "present": True,
                "referenced": 8192,
                "compression": "lz4",
                "snapshots": 1,
                "oldest_snapshot_at": HEARTBEAT - 60,
                "base": None,
                "bookmark": False,
            },
        ],
        "self_dataset": "found",
    },
    "job": {
        "id": 5,
        "state": "awaiting_confirm",
        "result": None,
        "reason": None,
        "snapshot": SNAPSHOT,
        "created_at": HEARTBEAT - 60,
        "deadline": HEARTBEAT + 1700,
        "confirmed": False,
        "confirm_reasons": ["deletions"],
        "send_ready": True,
        "blockers": [],
        "datasets": [
            {
                "name": "twinraidtest",
                "mode": "incremental",
                "base_guid": GUID,
                "base_type": "snapshot",
                "problem": None,
                "deleted": 1200,
                "modified": 3,
                "added": 0,
                "renamed": 1,
                "referenced": 4096,
                "base_referenced": 6000,
                "size": 70000,
                "sent": None,
                "result": None,
            },
            {
                "name": "dokumente",
                "mode": "full",
                "base_guid": None,
                "base_type": None,
                "problem": None,
                "deleted": None,
                "modified": None,
                "added": None,
                "renamed": None,
                "referenced": 8192,
                "base_referenced": None,
                "size": 9000,
                "sent": None,
                "result": None,
            },
        ],
    },
    "seq_exhausted": False,
}


def raw(data: dict[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()


def changed(path: str, value: Any) -> dict[str, Any]:  # noqa: ANN401 - beliebiger JSON-Wert
    data = copy.deepcopy(STATUS)
    target = data
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if isinstance(target, list):
        target[int(keys[-1])] = value
    else:
        target[keys[-1]] = value
    return data


# --- mountinfo -----------------------------------------------------------------------------


def mount_line(number: int, root: str, target: str, options: str = "rw,nosuid") -> str:
    return f"{number} 1 0:{number} {root} {target} {options} shared:1 - tmpfs tmpfs rw"


def mountinfo(base: Path, *, state: str | None = "ro", request: str | None = "rw") -> str:
    lines = [
        mount_line(20, "/", "/", "ro,relatime"),
        mount_line(21, f"/containers/{OWN_ID}/hostname", "/etc/hostname"),
        mount_line(22, f"/containers/{OWN_ID}/hosts", "/etc/hosts"),
        mount_line(23, f"/containers/{OWN_ID}/resolv.conf", "/etc/resolv.conf"),
    ]
    if state is not None:
        lines.append(
            mount_line(30, "/twinraid-source-guard/state", f"{base}/state", f"{state},nosuid")
        )
    if request is not None:
        lines.append(mount_line(31, "/request", f"{base}/request", f"{request},nosuid"))
    return "\n".join(lines) + "\n"


def guard_paths(tmp_path: Path, info_text: str | None = None) -> GuardPaths:
    base = tmp_path / "source-guard"
    (base / "state").mkdir(parents=True, exist_ok=True)
    if not (base / "request").exists():
        (base / "request").write_bytes(b"")
    info = tmp_path / "mountinfo"
    info.write_text(info_text if info_text is not None else mountinfo(base))
    return GuardPaths(base=base, mountinfo=info)
