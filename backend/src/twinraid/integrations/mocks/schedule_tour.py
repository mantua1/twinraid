"""Zeitplan und automatisches Herunterfahren im festen Rundlauf (Konzept 6.4, ab v0.6.0).

Mit twinraid-admin mock-tour zeigt die Simulation einen geplanten Lauf, der auf eine Sperre
wartet, einen ausgelassenen Termin und ein vorgemerktes Herunterfahren (mit Uhrzeit und mit
einem Grund), im selben Takt wie der Rundlauf von Groß und VM. Nur Anzeige: Der Zeitplan im
Worker läuft unverändert weiter, Aktionen sind während des Rundlaufs gesperrt (tour).
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from twinraid.integrations.mocks.lab import tour_flag
from twinraid.integrations.mocks.status import STEP_SECONDS
from twinraid.shared.actions import ActionBlocker, ActionOutcome
from twinraid.shared.schedule import (
    AutoShutdownKind,
    AutoShutdownReason,
    AutoShutdownStatus,
    ScheduleLast,
    ScheduleStatus,
    ScheduleWaiting,
)

_RETRY = timedelta(minutes=5)
_WINDOW = timedelta(minutes=60)


def _waiting(schedule: ScheduleStatus, now: datetime) -> ScheduleStatus:
    first = now - timedelta(minutes=10)
    return schedule.model_copy(
        update={
            "waiting": ScheduleWaiting(
                term=first,
                reason=ActionBlocker.SOURCE_GUARD_TOO_SOON,
                next_attempt_at=first + 3 * _RETRY,
                until=first + _WINDOW,
            )
        }
    )


def _skipped(schedule: ScheduleStatus, now: datetime) -> ScheduleStatus:
    term = now - timedelta(hours=1, minutes=10)
    return schedule.model_copy(
        update={
            "last": ScheduleLast(
                scheduled_for=term,
                outcome=ActionOutcome.SCHEDULE_SKIPPED,
                reason=ActionBlocker.SOURCE_GUARD_INACTIVE,
                finished_at=term + _WINDOW,
            )
        }
    )


def _succeeded(schedule: ScheduleStatus, now: datetime) -> ScheduleStatus:
    term = now - timedelta(minutes=20)
    return schedule.model_copy(
        update={
            "last": ScheduleLast(
                scheduled_for=term,
                outcome=ActionOutcome.SUCCESS,
                reason=None,
                finished_at=now - timedelta(minutes=2),
            )
        }
    )


# (Zeitplan, Vormerkung) je Schritt.
Step = Callable[[ScheduleStatus, datetime], tuple[ScheduleStatus, AutoShutdownStatus | None]]
SCHEDULE_SCENARIO: tuple[Step, ...] = (
    lambda schedule, now: (_waiting(schedule, now), None),
    lambda schedule, now: (_skipped(schedule, now), None),
    lambda schedule, now: (
        _succeeded(schedule, now),
        AutoShutdownStatus(
            kinds=(AutoShutdownKind.AFTER_SYNC,), at=now + timedelta(minutes=3), reason=None
        ),
    ),
    lambda schedule, now: (
        schedule,
        AutoShutdownStatus(
            kinds=(AutoShutdownKind.AFTER_PLAY,), at=None, reason=AutoShutdownReason.VM_RUNNING
        ),
    ),
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ScheduleTour:
    def __init__(
        self,
        flag_dir: Path,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._flag_dir = flag_dir
        self._monotonic = monotonic
        self._clock = clock
        self._started = monotonic()

    def step(self) -> int:
        return int((self._monotonic() - self._started) // STEP_SECONDS) % len(SCHEDULE_SCENARIO)

    def __call__(
        self, schedule: ScheduleStatus, auto: AutoShutdownStatus | None
    ) -> tuple[ScheduleStatus, AutoShutdownStatus | None]:
        if not tour_flag(self._flag_dir).exists():
            return schedule, auto
        shown = schedule.model_copy(update={"enabled": True, "permitted": True})
        return SCHEDULE_SCENARIO[self.step()](shown, self._clock().replace(microsecond=0))
