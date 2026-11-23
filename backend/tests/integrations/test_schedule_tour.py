"""Zeitplan und Vormerkung im festen Rundlauf der Simulation (Konzept 6.4, ab v0.6.0)."""

from datetime import UTC, datetime
from pathlib import Path

from tests.web.helpers import SCHEDULE_OFF
from twinraid.integrations.mocks.lab import set_flag, tour_flag
from twinraid.integrations.mocks.schedule_tour import SCHEDULE_SCENARIO, ScheduleTour
from twinraid.integrations.mocks.status import STEP_SECONDS
from twinraid.shared.actions import ActionOutcome

NOW = datetime(2026, 11, 29, 2, 0, tzinfo=UTC)


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_without_tour_nothing_changes(tmp_path: Path) -> None:
    tour = ScheduleTour(tmp_path, clock=lambda: NOW)
    assert tour(SCHEDULE_OFF, None) == (SCHEDULE_OFF, None)


def test_tour_shows_every_schedule_state(tmp_path: Path) -> None:
    """Wartender Lauf, ausgelassener Termin, vorgemerktes Herunterfahren mit Uhrzeit und mit
    Grund: jeder Zustand mindestens einmal (Konzept 6.4)."""
    ticks = Ticks()
    tour = ScheduleTour(tmp_path, monotonic=ticks, clock=lambda: NOW)
    set_flag(tour_flag(tmp_path), True)
    seen_waiting = seen_skipped = seen_at = seen_reason = False
    for step in range(len(SCHEDULE_SCENARIO)):
        ticks.now = step * STEP_SECONDS
        schedule, auto = tour(SCHEDULE_OFF, None)
        assert (schedule.enabled, schedule.permitted) == (True, True)
        seen_waiting |= schedule.waiting is not None
        seen_skipped |= (
            schedule.last is not None and schedule.last.outcome is ActionOutcome.SCHEDULE_SKIPPED
        )
        seen_at |= auto is not None and auto.at is not None
        seen_reason |= auto is not None and auto.reason is not None
    assert (seen_waiting, seen_skipped, seen_at, seen_reason) == (True, True, True, True)
    # Danach beginnt der Rundlauf von vorn.
    ticks.now = len(SCHEDULE_SCENARIO) * STEP_SECONDS
    assert tour(SCHEDULE_OFF, None)[0].waiting is not None
