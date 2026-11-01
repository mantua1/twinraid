"""Nachgebauter Wächter für den Starttest (Konzept 3.6, Protokoll 2).

Aufruf:
  fake_source_guard.py once <Ordner>   schreibt einmal <Ordner>/state/status.json (0644)
  fake_source_guard.py serve <Ordner>  läuft als root wie der Wächter und nimmt close an

serve legt Statusordner (0700), status.json (0600) und Auftragsdatei (0600) so an, dass nur
root sie lesen kann, und meldet einen offenen Job. Der Worker schließt ihn nach seinem Start
selbst (Konzept 3.6, „TwinRaid startet neu“). So zeigt der Test, dass ein Worker ohne
Capabilities Status und Aufträge nur als Besitzer root erreicht, ohne DAC_READ_SEARCH.
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ORDER = re.compile(r"^TRSG1 ([1-9][0-9]{0,8}) ([a-z]+) ([0-9a-f]{16}) *\n?$")


def status(now: int, last_seq: int, last: Any, job: Any) -> dict[str, Any]:
    return {
        "protocol": 2,
        "version": 2,
        "heartbeat_at": now,
        "started_at": now - 60,
        "instance": "0123456789abcdef",
        "error": None,
        "error_detail": None,
        "config_line": None,
        "last_seq": last_seq,
        "last": last,
        "rejected": {"count": 0, "last_at": None},
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
        "target": {"configured": False, "port": 22, "key": False, "known_host": False, "check": None},
        "allowed": [{"name": "twinraid-testcontainer", "id": None, "state": "missing"}],
        "run": None,
        "run_allowed_at": None,
        "last_run": None,
        "zfs": {
            "state": "unavailable",
            "pool": None,
            "datasets": [
                {
                    "name": "twinraidtest",
                    "present": None,
                    "referenced": None,
                    "compression": None,
                    "snapshots": None,
                    "oldest_snapshot_at": None,
                    "base": None,
                    "bookmark": None,
                }
            ],
            "self_dataset": "unknown",
        },
        "job": job,
        "seq_exhausted": False,
    }


def write_status(path: Path, content: dict[str, Any], mode: int) -> None:
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="ascii") as handle:
        json.dump(content, handle, separators=(",", ":"))
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def open_job(now: int) -> dict[str, Any]:
    return {
        "id": 1,
        "state": "snapshot",
        "result": None,
        "reason": None,
        "snapshot": "twinraid-20261101T120000Z",
        "created_at": now - 30,
        "deadline": now + 1800,
        "confirmed": False,
        "confirm_reasons": [],
        "send_ready": None,
        "blockers": [],
        "datasets": [],
    }


def read_order(path: Path, last_seq: int) -> tuple[int, str] | None:
    """Nummer und Auftrag, wenn in der Datei ein neuer Auftrag mit gültiger Prüfsumme steht."""
    match = ORDER.match(path.read_bytes().decode("ascii", errors="replace"))
    if match is None or int(match[1]) <= last_seq:
        return None
    payload = f"TRSG1 {match[1]} {match[2]}"
    if hashlib.sha256(payload.encode("ascii")).hexdigest()[:16] != match[3]:
        return None
    return int(match[1]), match[2]


def serve(root: Path) -> None:
    os.umask(0o077)
    state = root / "state"
    request = root / "request"
    state.mkdir(mode=0o700)
    # Erst den ersten Status, dann die Auftragsdatei: auf sie wartet der Test.
    now = int(time.time())
    job = open_job(now)
    write_status(state / "status.json", status(now, 0, None, job), 0o600)
    os.close(os.open(request, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    last_seq = 0
    last = None
    while True:
        now = int(time.time())
        order = read_order(request, last_seq)
        if order is not None:
            last_seq, name = order
            result = "not_supported"
            if name == "close":
                job = {**job, "state": "closed", "result": "closed", "deadline": None}
                result = "closed"
            last = {"seq": last_seq, "order": name, "container": None, "result": result, "at": now}
        write_status(state / "status.json", status(now, last_seq, last, job), 0o600)
        time.sleep(0.5)


def main() -> None:
    command, root = sys.argv[1], Path(sys.argv[2])
    if command == "once":
        now = int(time.time())
        write_status(root / "state" / "status.json", status(now, 0, None, None), 0o644)
    else:
        serve(root)


main()
