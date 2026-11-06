"""Sync-Historie des Workers unter /data/worker (Konzept 5.3, ab v0.5.2).

Eine JSON-Zeile je Lauf (Probelauf oder Sync), Rechte und Umbenennen wie beim Aktionslog:
Ordner und Datei gehören root und sind nur für root lesbar, ab 1 MiB wird die Datei nach
sync-history.jsonl.1 umbenannt. Alle Werte stammen aus festen Listen oder aus dem streng
geprüften Status des Wächters; Share-Namen kommen aus der Konfiguration bzw. vom Wächter.
Snapshot- und Container-Namen stehen nie in der Historie.
"""

import contextlib
import logging
import os
from collections import deque
from pathlib import Path

from pydantic import ValidationError

from twinraid.shared.sync import SyncHistoryEntry
from twinraid.worker.action_log import MAX_LOG_BYTES, rotated

logger = logging.getLogger(__name__)

# So viele Läufe zeigt die Kachel Sync über get_status.
RECENT = 20


def read_history(path: Path, limit: int) -> list[SyncHistoryEntry]:
    """Die letzten Läufe aus beiden Dateien, älteste zuerst; kaputte Zeilen fallen weg."""
    entries: list[SyncHistoryEntry] = []
    for source in (rotated(path), path):
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("could not read the sync history %s", source)
            continue
        for line in lines:
            try:
                entries.append(SyncHistoryEntry.model_validate_json(line))
            except ValidationError:
                continue
    return entries[-limit:] if limit > 0 else []


class SyncHistory:
    """path None: nur im Speicher (Tests ohne Datenablage)."""

    def __init__(self, path: Path | None, max_bytes: int = MAX_LOG_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        recent = read_history(path, RECENT) if path is not None else []
        self._recent: deque[SyncHistoryEntry] = deque(recent, maxlen=RECENT)

    def recent(self) -> tuple[SyncHistoryEntry, ...]:
        """Die letzten Läufe, neueste zuerst."""
        return tuple(reversed(self._recent))

    def append(self, entry: SyncHistoryEntry) -> None:
        """Schreibt eine Zeile. Ein Schreibfehler stoppt nie den Lauf selbst."""
        self._recent.append(entry)
        if self._path is None:
            return
        try:
            self._append(self._path, entry.model_dump_json() + "\n")
        except OSError:
            logger.exception("could not write the sync history %s", self._path)

    def _append(self, path: Path, line: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            if path.lstat().st_size >= self._max_bytes:
                path.replace(rotated(path))
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
