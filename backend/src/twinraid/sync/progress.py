"""Fortschritt und Restzeit beim Senden (Konzept 4.4, ab v0.5.2).

Der Worker liest den Status des Wächters alle 2 s und merkt sich je neuem Wert die Summe der
gesendeten Bytes über alle Shares; fertige Shares zählen mit ihrer Größe. Die Rate ist der
gleitende Durchschnitt über die Werte der letzten 60 s; Restzeit und Ende rechnet er alle
15 s neu. Weil die Größe aus prepare geschätzt ist, zeigt die Anzeige höchstens 99 %, bis
der Job fertig ist.
"""

from collections import deque
from datetime import datetime, timedelta

from twinraid.shared.source_guard import GuardDatasetResult, GuardJobDataset
from twinraid.shared.sync import SyncProgress

WINDOW_SECONDS = 60.0
RECOMPUTE_SECONDS = 15.0


def total_bytes(datasets: tuple[GuardJobDataset, ...]) -> int:
    return sum(item.size or 0 for item in datasets)


def sent_bytes(datasets: tuple[GuardJobDataset, ...]) -> int:
    """Gesendet über alle Shares; eine fertige Share zählt mit ihrer Größe."""
    total = 0
    for item in datasets:
        if item.result is GuardDatasetResult.OK and item.size is not None:
            total += item.size
        else:
            total += item.sent or 0
    return total


def current_share(datasets: tuple[GuardJobDataset, ...]) -> str | None:
    """Der Wächter sendet die Shares nacheinander: die erste ohne Ergebnis."""
    return next((item.name for item in datasets if item.result is None), None)


class ProgressTracker:
    """Rate, Restzeit und Ende; Zeiten in Sekunden der monotonen Uhr des Workers."""

    def __init__(
        self, window: float = WINDOW_SECONDS, recompute: float = RECOMPUTE_SECONDS
    ) -> None:
        self._window = window
        self._recompute = recompute
        self._samples: deque[tuple[float, int]] = deque()
        self._first_at: float | None = None
        self._computed_at: float | None = None
        self._rate: float | None = None
        self._remaining: int | None = None
        self._finish_at: datetime | None = None
        self._share: str | None = None
        self._sent = 0
        self._total = 0
        self._done = False

    def update(
        self,
        now: float,
        wall: datetime,
        datasets: tuple[GuardJobDataset, ...],
        done: bool = False,
    ) -> None:
        sent, total = sent_bytes(datasets), total_bytes(datasets)
        self._share, self._total, self._done = current_share(datasets), total, done
        if not self._samples or sent != self._samples[-1][1]:
            self._samples.append((now, sent))
        self._sent = sent
        if self._first_at is None:
            self._first_at = now
        while len(self._samples) > 1 and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        due = self._computed_at is None or now - self._computed_at >= self._recompute
        if now - self._first_at >= self._recompute and due:
            self._computed_at = now
            self._estimate(now, wall)

    def _estimate(self, now: float, wall: datetime) -> None:
        first_at, first_sent = self._samples[0]
        # Der letzte Wert gilt bis jetzt: Bleibt die Übertragung stehen, sinkt die Rate.
        elapsed = now - first_at
        if elapsed <= 0:
            return
        self._rate = (self._sent - first_sent) / elapsed
        if self._rate <= 0:
            self._remaining, self._finish_at = None, None
            return
        remaining = max(self._total - self._sent, 0) / self._rate
        self._remaining = round(remaining)
        self._finish_at = (wall + timedelta(seconds=remaining)).replace(microsecond=0)

    def snapshot(self) -> SyncProgress:
        if self._done:
            percent = 100
        elif self._total > 0:
            percent = min(99, self._sent * 100 // self._total)
        else:
            percent = 0
        return SyncProgress(
            share=None if self._done else self._share,
            sent=self._sent,
            total=self._total,
            percent=percent,
            rate=self._rate,
            remaining_seconds=None if self._done else self._remaining,
            finish_at=None if self._done else self._finish_at,
        )
