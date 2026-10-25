"""Läuft gerade ein Kopierjob? (Konzept 4.1 und 5.3).

Ab v0.5.1 beantwortet das der Probelauf im Worker (sync/coordinator.py): ein laufender
Probelauf oder ein offener Job beim Wächter.
"""

from typing import Protocol


class JobState(Protocol):
    def is_running(self) -> bool: ...
