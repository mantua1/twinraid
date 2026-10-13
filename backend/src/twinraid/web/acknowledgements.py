"""Bestätigte Hinweise in der Oberfläche (Konzept 5.2).

Bisher nur der Hinweis zum CA-Wechsel. Eine Bestätigung gilt für genau einen
Bezug (bei der CA ihren Fingerabdruck); eine spätere neue CA zeigt den Hinweis
wieder an.
"""

import time
from collections.abc import Callable
from typing import Literal

from twinraid.web.db import WebDatabase

AcknowledgementKind = Literal["ca_change"]


class Acknowledgements:
    def __init__(self, db: WebDatabase, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    def is_acknowledged(self, kind: AcknowledgementKind, ref: str) -> bool:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM acknowledgements WHERE kind = ? AND ref = ?", (kind, ref)
            ).fetchone()
        return row is not None

    def acknowledge(self, kind: AcknowledgementKind, ref: str) -> bool:
        """Gibt False zurück, wenn der Hinweis schon bestätigt war."""
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO acknowledgements (kind, ref, at) VALUES (?, ?, ?)",
                (kind, ref, self._clock()),
            )
            return cursor.rowcount == 1
