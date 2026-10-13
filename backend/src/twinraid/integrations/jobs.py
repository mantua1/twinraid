"""Läuft gerade ein Kopierjob? (Konzept 4.1 und 5.3).

Der echte Sync folgt in Phase 5. Bis dahin läuft im Modus real nie ein Job.
"""

from typing import Protocol


class JobState(Protocol):
    def is_running(self) -> bool: ...


class NoJobs:
    def is_running(self) -> bool:
        return False
