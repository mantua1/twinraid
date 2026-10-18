"""Simulierter Wächter (Konzept 6.4): Standard, Fehler und Rundlauf."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from twinraid.integrations.mocks.lab import fault_flag, set_flag, tour_flag
from twinraid.integrations.mocks.source_guard import (
    GUARD_SCENARIO,
    MOCK_CONTAINER,
    MOCK_DATASET,
    MockSourceGuard,
)
from twinraid.integrations.mocks.status import STEP_SECONDS
from twinraid.shared.source_guard import (
    GuardContainerState,
    GuardError,
    GuardRestart,
    GuardRunEnd,
    GuardSelfDataset,
    GuardSetupProblem,
    GuardState,
    GuardStatusProblem,
    GuardZfsState,
)
from twinraid.shared.status import StatusNotice

NOW = datetime(2026, 10, 19, 12, 0, 2, tzinfo=UTC)


def _guard(tmp_path: Path, now: list[datetime], ticks: list[float]) -> MockSourceGuard:
    return MockSourceGuard(tmp_path, monotonic=lambda: ticks[0], clock=lambda: now[0])


def _switch(tmp_path: Path, name: str, at: datetime) -> None:
    flag = fault_flag(tmp_path, name)
    set_flag(flag, True)
    os.utime(flag, (at.timestamp(), at.timestamp()))


def test_default_is_active(tmp_path: Path) -> None:
    guard = _guard(tmp_path, [NOW], [0.0])
    report = guard.report()
    status = report.status
    assert status.state is GuardState.ACTIVE
    # Der Wächter schreibt alle 5 Sekunden: 12:00:00, Alter 2 s.
    assert status.heartbeat_at == NOW - timedelta(seconds=2)
    assert status.heartbeat_age_seconds == 2
    details = status.details
    assert details is not None
    assert [(item.name, item.state) for item in details.allowed] == [
        (MOCK_CONTAINER, GuardContainerState.RUNNING)
    ]
    assert [(item.name, item.compression) for item in details.zfs.datasets] == [
        (MOCK_DATASET, "on")
    ]
    assert details.run is None
    assert details.run_allowed_at is None
    assert report.notices == ()


def test_fault_inactive_since_switching_it_on(tmp_path: Path) -> None:
    since = NOW - timedelta(minutes=3)
    _switch(tmp_path, "source-guard-inactive", since)
    status = _guard(tmp_path, [NOW], [0.0]).report().status
    assert status.state is GuardState.INACTIVE
    assert status.heartbeat_at == since
    assert status.details is None


def test_fault_lost(tmp_path: Path) -> None:
    _switch(tmp_path, "source-guard-lost", NOW)
    status = _guard(tmp_path, [NOW], [0.0]).report().status
    assert status.state is GuardState.ERROR
    assert status.error is GuardError.STATE_LOST
    assert status.details is not None


def test_fault_start_failed_then_pause(tmp_path: Path) -> None:
    closed = NOW - timedelta(minutes=1)
    _switch(tmp_path, "source-guard-start-failed", closed)
    now = [NOW]
    guard = _guard(tmp_path, now, [0.0])
    details = guard.report().status.details
    assert details is not None
    assert details.last_run is not None
    assert details.last_run.closed_at == closed
    assert [item.result for item in details.last_run.containers] == [GuardRestart.START_FAILED]
    assert details.allowed[0].state is GuardContainerState.STOPPED
    assert details.run_allowed_at == closed + timedelta(minutes=30)
    # Nach der Pause gibt es keinen frühesten Zeitpunkt mehr.
    now[0] = closed + timedelta(minutes=31)
    details = guard.report().status.details
    assert details is not None
    assert details.run_allowed_at is None
    assert details.last_run is not None


def test_faults_combined(tmp_path: Path) -> None:
    _switch(tmp_path, "source-guard-lost", NOW)
    _switch(tmp_path, "source-guard-start-failed", NOW)
    status = _guard(tmp_path, [NOW], [0.0]).report().status
    assert status.error is GuardError.STATE_LOST
    assert status.details is not None
    assert status.details.last_run is not None
    _switch(tmp_path, "source-guard-inactive", NOW)
    assert _guard(tmp_path, [NOW], [0.0]).report().status.state is GuardState.INACTIVE


def test_tour_shows_every_state(tmp_path: Path) -> None:
    """Konzept 6.4: jeder Zustand, jeder Fehler und jeder Hinweis des Wächters."""
    set_flag(tour_flag(tmp_path), True)
    now = [NOW]
    ticks = [0.0]
    guard = _guard(tmp_path, now, ticks)
    states: set[GuardState] = set()
    setup: set[GuardSetupProblem] = set()
    problems: set[GuardStatusProblem] = set()
    errors: set[GuardError] = set()
    containers: set[GuardContainerState] = set()
    ends: set[GuardRunEnd] = set()
    restarts: set[GuardRestart] = set()
    self_datasets: set[GuardSelfDataset] = set()
    zfs_states: set[GuardZfsState] = set()
    notices: set[StatusNotice] = set()
    compressions: set[str | None] = set()
    seen_run = seen_pause = seen_never = False
    for step in range(len(GUARD_SCENARIO)):
        ticks[0] = step * STEP_SECONDS + 1
        now[0] = NOW + timedelta(seconds=step * STEP_SECONDS + 1)
        assert guard.tour_step() == step
        report = guard.report()
        status = report.status
        notices.update(report.notices)
        states.add(status.state)
        if status.setup_problem is not None:
            setup.add(status.setup_problem)
        if status.status_problem is not None:
            problems.add(status.status_problem)
        if status.error is not None:
            errors.add(status.error)
        if status.state is GuardState.INACTIVE and status.heartbeat_at is None:
            seen_never = True
        details = status.details
        if details is None:
            continue
        containers.update(item.state for item in details.allowed)
        self_datasets.add(details.zfs.self_dataset)
        zfs_states.add(details.zfs.state)
        compressions.update(item.compression for item in details.zfs.datasets)
        if details.run is not None:
            seen_run = True
        if details.run_allowed_at is not None:
            seen_pause = True
        if details.last_run is not None:
            ends.add(details.last_run.reason)
            restarts.update(item.result for item in details.last_run.containers)
    assert states == set(GuardState)
    assert setup == set(GuardSetupProblem)
    assert problems == set(GuardStatusProblem)
    assert errors == set(GuardError)
    assert containers == set(GuardContainerState)
    assert ends == set(GuardRunEnd)
    assert restarts == set(GuardRestart)
    assert self_datasets == set(GuardSelfDataset)
    assert zfs_states == set(GuardZfsState)
    assert notices == {
        StatusNotice.SOURCE_CONNECT_PLUGIN,
        StatusNotice.SOURCE_FLASH_BACKUP,
        StatusNotice.SOURCE_GUARD_ALLOWS_SELF,
    }
    assert {"on", "lz4", "off", None} <= compressions
    assert seen_run
    assert seen_pause
    assert seen_never
    # Danach beginnt der Rundlauf von vorn.
    ticks[0] = len(GUARD_SCENARIO) * STEP_SECONDS
    assert guard.tour_step() == 0


def test_tour_ends_with_the_flag(tmp_path: Path) -> None:
    set_flag(tour_flag(tmp_path), True)
    ticks = [0.0]
    guard = _guard(tmp_path, [NOW], ticks)
    ticks[0] = STEP_SECONDS + 1
    assert guard.report().status.state is GuardState.NOT_MOUNTED
    set_flag(tour_flag(tmp_path), False)
    assert guard.report().status.state is GuardState.ACTIVE
