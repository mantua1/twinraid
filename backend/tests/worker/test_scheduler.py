"""Zeitplan im Worker mit nachgebauter Uhr (Konzept 4.8, Entscheidung E29, ab v0.6.0)."""

import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import EXAMPLE_CONFIG
from tests.support.guard_status import NOW as GUARD_NOW
from tests.support.guard_status import STATUS, guard_paths, raw
from twinraid.config import TwinRaidConfig
from twinraid.integrations.source_guard import GuardReport, evaluate, read_observation
from twinraid.shared.actions import ActionBlocker, ActionOutcome, Trigger
from twinraid.shared.mode import RunMode
from twinraid.shared.schedule import ScheduleLimit
from twinraid.shared.source_guard import GuardState, SourceGuardStatus
from twinraid.shared.status import ResourceInfo, ResourceState, ResourceStatus
from twinraid.sync.schedule import ScheduleState
from twinraid.worker.action_log import ActionLog
from twinraid.worker.scheduler import Scheduler
from twinraid.worker.sync_history import SyncHistory

pytestmark = pytest.mark.anyio

# Sonntag, 29.11.2026, 03:00 in Europe/Berlin.
TERM = datetime(2026, 11, 29, 2, 0, tzinfo=UTC)
ON = ResourceInfo(state=ResourceState.ONLINE)
OFF = ResourceInfo(state=ResourceState.OFFLINE)


def _config(
    days: list[str] | None = None,
    actions: dict[str, bool] | None = None,
    enabled: bool = True,
    wake_target: bool = True,
    stop_containers: bool = True,
) -> TwinRaidConfig:
    # Any: verschachtelte YAML-Daten werden im Test gezielt verändert.
    data: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["sync"]["schedule"].update(enabled=enabled, wake_target=wake_target)
    data["sync"]["stop_containers"] = stop_containers
    if days is not None:
        data["sync"]["schedule"]["days"] = days
    if actions is not None:
        data["actions"].update(actions)
    return TwinRaidConfig.model_validate(data)


class FakeSync:
    def __init__(self) -> None:
        self.terms: list[datetime] = []
        self.answer: ActionBlocker | None = None

    async def request_scheduled(self, term: datetime) -> ActionBlocker | None:
        self.terms.append(term)
        return self.answer


class StubStatus:
    def __init__(self) -> None:
        self.target = ON

    def current(self) -> ResourceStatus:
        return ResourceStatus(source=ON, target=self.target, vm=OFF, checked_at=None)


class StubGuard:
    def __init__(self, report: GuardReport | None = None) -> None:
        self.value = report or GuardReport(SourceGuardStatus(state=GuardState.NOT_MOUNTED))

    def report(self) -> GuardReport:
        return self.value


def _guard(tmp_path: Path, **limits: int) -> StubGuard:
    data = copy.deepcopy(STATUS)
    data["limits"].update(limits)
    paths = guard_paths(tmp_path / "guard")
    paths.status_file.write_bytes(raw(data))
    return StubGuard(evaluate(read_observation(paths), GUARD_NOW))


class Plan:
    """Zeitplan mit nachgebauter Uhr; der Worker startete start vor dem Termin."""

    def __init__(
        self,
        tmp_path: Path,
        config: TwinRaidConfig | None = None,
        mode: RunMode = RunMode.MOCK,
        start: datetime = TERM - timedelta(hours=1),
        guard: StubGuard | None = None,
    ) -> None:
        self.dir = tmp_path
        self.now = start
        self.config = config or _config()
        self.mode = mode
        self.sync = FakeSync()
        self.blocker: ActionBlocker | None = None
        self.status = StubStatus()
        self.guard = guard or StubGuard()
        self.state_path = tmp_path / "schedule.json"
        self.log_path = tmp_path / "actions.jsonl"
        self.history = SyncHistory(tmp_path / "sync-history.jsonl")
        self.scheduler = self.make()

    def make(self) -> Scheduler:
        return Scheduler(
            self.config,
            self.mode,
            self.sync,
            lambda: self.blocker,
            self.status,
            self.guard,
            ScheduleState(self.state_path),
            ActionLog(self.log_path, clock=lambda: self.now),
            self.history,
            clock=lambda: self.now,
        )

    async def at(self, moment: datetime) -> None:
        self.now = moment
        await self.scheduler.tick()

    def log(self) -> list[tuple[str, str, str, str | None]]:
        if not self.log_path.exists():
            return []
        entries = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [(e["action"], e["trigger"], e["result"], e.get("reason")) for e in entries]


@pytest.fixture
def plan(tmp_path: Path) -> Plan:
    return Plan(tmp_path)


# --- Termin und Start ---


async def test_term_starts_the_run(plan: Plan) -> None:
    await plan.at(TERM - timedelta(seconds=1))
    assert plan.sync.terms == []
    await plan.at(TERM + timedelta(seconds=5))
    assert plan.sync.terms == [TERM]
    assert plan.scheduler.snapshot().waiting is None
    assert json.loads(plan.state_path.read_text()) == {"last_handled": "2026-11-29T02:00:00Z"}
    # Nie zweimal.
    await plan.at(TERM + timedelta(minutes=10))
    assert plan.sync.terms == [TERM]


async def test_not_before_two_minutes_after_the_start(tmp_path: Path) -> None:
    plan = Plan(tmp_path, start=TERM - timedelta(seconds=30))
    await plan.at(TERM + timedelta(seconds=10))
    assert plan.sync.terms == []
    await plan.at(TERM + timedelta(seconds=89))
    assert plan.sync.terms == []
    await plan.at(TERM + timedelta(seconds=90))
    assert plan.sync.terms == [TERM]


async def test_restart_does_not_repeat_a_term(plan: Plan) -> None:
    await plan.at(TERM + timedelta(seconds=5))
    again = plan.make()
    plan.now = TERM + timedelta(minutes=30)
    await again.tick()
    assert plan.sync.terms == [TERM]


async def test_term_before_the_first_start_is_not_missed(tmp_path: Path) -> None:
    """Ohne schedule.json gilt kein früherer Termin als verpasst (Konzept 4.8)."""
    plan = Plan(tmp_path, start=TERM + timedelta(minutes=30))
    await plan.at(TERM + timedelta(minutes=40))
    assert plan.sync.terms == []
    assert plan.log() == []


async def test_unreadable_state_counts_like_a_missing_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "schedule.json").write_text("{kaputt")
    with caplog.at_level(logging.ERROR):
        plan = Plan(tmp_path, start=TERM + timedelta(minutes=30))
    assert "schedule.json is not readable" in caplog.text
    await plan.at(TERM + timedelta(minutes=40))
    assert plan.sync.terms == []


# --- Verpasst und nachgeholt ---


async def test_catch_up_within_six_hours(tmp_path: Path) -> None:
    ScheduleState(tmp_path / "schedule.json").save(TERM - timedelta(days=7))
    plan = Plan(tmp_path, start=TERM + timedelta(hours=5))
    await plan.at(TERM + timedelta(hours=5, minutes=2))
    assert plan.sync.terms == [TERM]


async def test_missed_after_six_hours(tmp_path: Path) -> None:
    ScheduleState(tmp_path / "schedule.json").save(TERM - timedelta(days=7))
    plan = Plan(tmp_path, start=TERM + timedelta(hours=7))
    await plan.at(TERM + timedelta(hours=7, minutes=2))
    assert plan.sync.terms == []
    assert plan.log() == [("sync_start", "schedule", "schedule_missed", None)]
    entry = plan.history.recent()[0]
    assert (entry.trigger, entry.result, entry.scheduled_for, entry.shares) == (
        Trigger.SCHEDULE,
        ActionOutcome.SCHEDULE_MISSED,
        TERM,
        (),
    )
    last = plan.scheduler.snapshot().last
    assert last is not None
    assert (last.scheduled_for, last.outcome) == (TERM, ActionOutcome.SCHEDULE_MISSED)
    # Der verpasste Termin ist behandelt; er kommt nie wieder.
    await plan.at(TERM + timedelta(hours=8))
    assert len(plan.log()) == 1


async def test_clock_jump_back_does_not_repeat(plan: Plan) -> None:
    await plan.at(TERM + timedelta(seconds=5))
    await plan.at(TERM - timedelta(hours=1))
    await plan.at(TERM + timedelta(seconds=5))
    assert plan.sync.terms == [TERM]


async def test_clock_jump_forward_catches_up_once(tmp_path: Path) -> None:
    daily = _config(days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    plan = Plan(tmp_path, config=daily, start=TERM - timedelta(hours=1))
    # Die Uhr springt über zwei Termine: einmal, mit dem jüngsten.
    await plan.at(TERM + timedelta(days=1, hours=2))
    assert plan.sync.terms == [TERM + timedelta(days=1)]


# --- Sperren ---


async def test_blocked_term_retries_every_five_minutes(plan: Plan) -> None:
    plan.blocker = ActionBlocker.SOURCE_GUARD_TOO_SOON
    first = TERM + timedelta(seconds=5)
    await plan.at(first)
    waiting = plan.scheduler.snapshot().waiting
    assert waiting is not None
    assert (waiting.term, waiting.reason) == (TERM, ActionBlocker.SOURCE_GUARD_TOO_SOON)
    assert waiting.next_attempt_at == first + timedelta(minutes=5)
    assert waiting.until == first + timedelta(minutes=60)
    await plan.at(first + timedelta(minutes=4))
    plan.blocker = ActionBlocker.BUSY
    await plan.at(first + timedelta(minutes=5))
    waiting = plan.scheduler.snapshot().waiting
    assert waiting is not None
    assert waiting.reason is ActionBlocker.BUSY
    plan.blocker = None
    await plan.at(first + timedelta(minutes=10))
    assert plan.sync.terms == [TERM]
    assert plan.scheduler.snapshot().waiting is None
    assert plan.log() == []


async def test_blocked_for_an_hour_is_skipped(plan: Plan) -> None:
    plan.blocker = ActionBlocker.SOURCE_GUARD_INACTIVE
    first = TERM + timedelta(seconds=5)
    moment = first
    while moment < first + timedelta(minutes=60):
        await plan.at(moment)
        assert plan.scheduler.snapshot().waiting is not None
        moment += timedelta(seconds=10)
    await plan.at(first + timedelta(minutes=60))
    assert plan.scheduler.snapshot().waiting is None
    assert plan.sync.terms == []
    assert plan.log() == [("sync_start", "schedule", "schedule_skipped", "source_guard_inactive")]
    entry = plan.history.recent()[0]
    assert (entry.result, entry.reason, entry.scheduled_for) == (
        ActionOutcome.SCHEDULE_SKIPPED,
        ActionBlocker.SOURCE_GUARD_INACTIVE,
        TERM,
    )


async def test_retries_count_from_the_first_attempt_after_a_catch_up(tmp_path: Path) -> None:
    """Präzisierung in Konzept 0.20: 60 Minuten ab dem ersten Versuch, nicht ab dem Termin."""
    ScheduleState(tmp_path / "schedule.json").save(TERM - timedelta(days=7))
    plan = Plan(tmp_path, start=TERM + timedelta(hours=3))
    plan.blocker = ActionBlocker.BUSY
    first = TERM + timedelta(hours=3, minutes=2)
    await plan.at(first)
    waiting = plan.scheduler.snapshot().waiting
    assert waiting is not None
    assert waiting.until == first + timedelta(minutes=60)
    plan.blocker = None
    await plan.at(first + timedelta(minutes=5))
    assert plan.sync.terms == [TERM]


async def test_refusal_at_start_counts_as_a_blocker(plan: Plan) -> None:
    """Ändert sich der Stand zwischen Prüfung und Start, gilt die Antwort des Syncs."""
    plan.sync.answer = ActionBlocker.BUSY
    await plan.at(TERM + timedelta(seconds=5))
    waiting = plan.scheduler.snapshot().waiting
    assert waiting is not None
    assert waiting.reason is ActionBlocker.BUSY


@pytest.mark.parametrize(
    ("config", "mode", "target", "reason"),
    [
        # Im Modus real ohne actions.sync: sofort aus.
        (_config(), RunMode.REAL, ON, ActionBlocker.NOT_ENABLED),
        # Groß ist aus und wake_target ist aus.
        (_config(wake_target=False), RunMode.MOCK, OFF, ActionBlocker.TARGET_OFFLINE),
        # Groß ist aus und actions.wake fehlt.
        (_config(actions={"sync": True}), RunMode.REAL, OFF, ActionBlocker.TARGET_OFFLINE),
    ],
)
async def test_skipped_at_once(
    tmp_path: Path,
    config: TwinRaidConfig,
    mode: RunMode,
    target: ResourceInfo,
    reason: ActionBlocker,
) -> None:
    plan = Plan(tmp_path, config=config, mode=mode)
    plan.status.target = target
    await plan.at(TERM + timedelta(seconds=5))
    assert plan.sync.terms == []
    assert plan.scheduler.snapshot().waiting is None
    assert plan.log() == [("sync_start", "schedule", "schedule_skipped", reason.value)]


async def test_not_enabled_from_the_availability_skips_at_once(plan: Plan) -> None:
    plan.blocker = ActionBlocker.NOT_ENABLED
    await plan.at(TERM + timedelta(seconds=5))
    assert plan.log() == [("sync_start", "schedule", "schedule_skipped", "not_enabled")]


async def test_target_off_is_woken_with_wake_target(tmp_path: Path) -> None:
    plan = Plan(tmp_path, config=_config(actions={"sync": True, "wake": True}), mode=RunMode.REAL)
    plan.status.target = OFF
    await plan.at(TERM + timedelta(seconds=5))
    assert plan.sync.terms == [TERM]


# --- Aus, Anzeige, Takt ---


async def test_disabled_schedule_does_nothing(tmp_path: Path) -> None:
    plan = Plan(tmp_path, config=_config(enabled=False))
    await plan.scheduler.run()  # endet sofort
    status = plan.scheduler.snapshot()
    assert (status.enabled, status.next_terms) == (False, ())


def test_snapshot_shows_the_plan(tmp_path: Path) -> None:
    config = _config(actions={"sync": True, "wake": True})
    status = Plan(tmp_path, config=config, mode=RunMode.REAL).scheduler.snapshot()
    assert (status.enabled, status.permitted, status.wake, status.shutdown) == (
        True,
        True,
        True,
        False,
    )
    assert status.next_terms == (TERM, TERM + timedelta(days=7), TERM + timedelta(days=14))
    assert (status.timezone, status.days, status.time) == ("Europe/Berlin", ("sun",), "03:00")
    assert (status.shutdown_delay_minutes, status.play_delay_minutes) == (5, 15)
    assert Plan(tmp_path / "x", mode=RunMode.REAL).scheduler.snapshot().permitted is False


def test_start_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plan = Plan(tmp_path)
    with caplog.at_level(logging.INFO):
        plan.scheduler.log_start()
    assert (
        "Next scheduled run: Sun 2026-11-29 03:00 Europe/Berlin (02:00 UTC); "
        "wakes the target: yes; shuts it down afterwards: yes"
    ) in caplog.text


async def test_interval_conflict_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daily = _config(days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    guard = _guard(tmp_path, min_run_interval_minutes=1440, min_snapshot_interval_minutes=30)
    plan = Plan(tmp_path, config=daily, guard=guard)
    with caplog.at_level(logging.WARNING):
        await plan.at(TERM - timedelta(minutes=50))
        await plan.at(TERM - timedelta(minutes=40))
    assert caplog.text.count("schedule_interval_conflict") == 1
    status = plan.scheduler.snapshot()
    # Um die Zeitumstellung liegen nur 23 Stunden zwischen zwei Terminen.
    assert status.shortest_gap_minutes == 23 * 60
    assert [(c.limit, c.minutes) for c in status.conflicts] == [
        (ScheduleLimit.MIN_RUN_INTERVAL, 1440)
    ]


async def test_run_interval_counts_only_with_stopped_containers(tmp_path: Path) -> None:
    daily = _config(days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"], stop_containers=False)
    guard = _guard(tmp_path, min_run_interval_minutes=1440)
    plan = Plan(tmp_path, config=daily, guard=guard)
    await plan.at(TERM - timedelta(minutes=50))
    assert plan.scheduler.snapshot().conflicts == ()


async def test_weekly_schedule_has_no_conflict(tmp_path: Path) -> None:
    guard = _guard(tmp_path, min_run_interval_minutes=1440, min_snapshot_interval_minutes=1440)
    plan = Plan(tmp_path, guard=guard)
    await plan.at(TERM - timedelta(minutes=50))
    status = plan.scheduler.snapshot()
    assert status.conflicts == ()
    assert status.shortest_gap_minutes is not None
    assert status.shortest_gap_minutes >= 7 * 24 * 60 - 60
