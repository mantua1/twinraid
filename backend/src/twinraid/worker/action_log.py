"""Aktionslog des Workers unter /data/worker (Konzept 5.3).

Eine JSON-Zeile pro Eintrag: Zeitpunkt, ID, Aktion, Auslöser, Ergebnis. Alle
Werte stammen aus festen Listen. Ordner und Datei gehören root und sind nur für
root lesbar: Der Web-Prozess kann das Log weder lesen noch verändern. Ab 1 MiB
wird die Datei nach actions.jsonl.1 umbenannt (genau eine ältere Datei).
"""

import contextlib
import json
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from twinraid.shared.actions import ActionName, ActionOutcome, Trigger

logger = logging.getLogger(__name__)

MAX_LOG_BYTES = 1024 * 1024
# Ergebnis des ersten Eintrags einer Aktion; das Ende trägt ein ActionOutcome.
STARTED = "started"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def new_action_id() -> str:
    """Vom Worker vergebene ID, verbindet Beginn und Ende einer Aktion."""
    return secrets.token_hex(4)


def rotated(path: Path) -> Path:
    return path.with_name(path.name + ".1")


@dataclass(frozen=True)
class LogEntry:
    at: datetime
    id: str
    action: ActionName
    trigger: Trigger
    result: str
    part_of: ActionName | None = None

    def to_json(self) -> str:
        data: dict[str, str] = {
            "at": self.at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "id": self.id,
            "action": self.action.value,
            "trigger": self.trigger.value,
            "result": self.result,
        }
        if self.part_of is not None:
            data["part_of"] = self.part_of.value
        return json.dumps(data, separators=(",", ":"))


class ActionLog:
    def __init__(
        self,
        path: Path,
        clock: Callable[[], datetime] = _utc_now,
        max_bytes: int = MAX_LOG_BYTES,
    ) -> None:
        self._path = path
        self._clock = clock
        self._max_bytes = max_bytes

    def record(
        self,
        action_id: str,
        action: ActionName,
        trigger: Trigger,
        result: ActionOutcome | str,
        part_of: ActionName | None = None,
    ) -> None:
        """Schreibt einen Eintrag. Ein Schreibfehler stoppt nie die Aktion selbst."""
        entry = LogEntry(self._clock(), action_id, action, trigger, str(result), part_of)
        via = f" (part of {part_of.value})" if part_of is not None else ""
        logger.info(
            "action %s %s by %s%s: %s", action_id, action.value, trigger.value, via, entry.result
        )
        try:
            self._append(entry.to_json() + "\n")
        except OSError:
            logger.exception("could not write the action log %s", self._path)

    def _append(self, line: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            if self._path.lstat().st_size >= self._max_bytes:
                self._path.replace(rotated(self._path))
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self._path, flags, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def read_entries(path: Path, limit: int) -> list[dict[str, str]]:
    """Die letzten Einträge aus beiden Dateien, älteste zuerst; kaputte Zeilen fallen weg."""
    entries: list[dict[str, str]] = []
    for source in (rotated(path), path):
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                entries.append({str(k): str(v) for k, v in data.items()})
    return entries[-limit:] if limit > 0 else []
