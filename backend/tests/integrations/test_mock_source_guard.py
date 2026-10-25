"""Simulierter Wächter (Konzept 6.4): Standard, Fehler und Rundlauf."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from twinraid.integrations.guard_orders import GuardOrderError
from twinraid.integrations.mocks.guard_jobs import GIB, MIB
from twinraid.integrations.mocks.lab import fault_flag, set_flag, tour_flag
from twinraid.integrations.mocks.source_guard import (
    GUARD_SCENARIO,
    MOCK_CONTAINER,
    MOCK_DATASET,
    MockSourceGuard,
)
from twinraid.integrations.mocks.status import STEP_SECONDS
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheckResult,
    GuardConfirmReason,
    GuardContainerState,
    GuardDatasetProblem,
    GuardDetails,
    GuardError,
    GuardJob,
    GuardJobResult,
    GuardJobState,
    GuardOrder,
    GuardOrderResult,
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


# --- Aufträge und Job des Probelaufs (Konzept 6.4, ab v0.5.1) ---------------------------------

SHARES = ("appdata", "dokumente")


async def _no_sleep(_seconds: float) -> None:
    return None


def _job_guard(tmp_path: Path, now: list[datetime], ticks: list[float]) -> MockSourceGuard:
    return MockSourceGuard(
        tmp_path,
        monotonic=lambda: ticks[0],
        clock=lambda: now[0],
        sleep=_no_sleep,
        datasets=SHARES,
    )


def _details(guard: MockSourceGuard) -> GuardDetails:
    details = guard.report().status.details
    assert details is not None
    return details


async def _prepared(guard: MockSourceGuard, ticks: list[float]) -> GuardJob:
    assert await guard.order(GuardOrder.SNAPSHOT) is GuardOrderResult.CREATED
    assert await guard.order(GuardOrder.PREPARE) is GuardOrderResult.PREPARING
    ticks[0] += 6
    job = _details(guard).job
    assert job is not None
    return job


@pytest.mark.anyio
async def test_datasets_follow_the_shares(tmp_path: Path) -> None:
    details = _details(_job_guard(tmp_path, [NOW], [0.0]))
    assert [item.name for item in details.zfs.datasets] == list(SHARES)
    assert details.version == 2
    assert (details.target_configured, details.target_key, details.target_known_host) == (
        True,
        True,
        True,
    )
    assert details.last_seq == 0
    assert details.job is None
    assert details.target_check is None


@pytest.mark.anyio
async def test_dry_run_orders(tmp_path: Path) -> None:
    now, ticks = [NOW], [0.0]
    guard = _job_guard(tmp_path, now, ticks)
    assert await guard.order(GuardOrder.PREPARE) is GuardOrderResult.NO_JOB
    assert await guard.order(GuardOrder.SNAPSHOT) is GuardOrderResult.CREATED
    details = _details(guard)
    assert details.last_seq == 2
    assert details.last_order is not None
    assert (details.last_order.seq, details.last_order.order) == (2, GuardOrder.SNAPSHOT)
    job = details.job
    assert job is not None
    assert (job.id, job.state, job.snapshot) == (
        2,
        GuardJobState.SNAPSHOT,
        "twinraid-20261019T120002Z",
    )
    assert [item.name for item in job.datasets] == list(SHARES)
    assert await guard.order(GuardOrder.SNAPSHOT) is GuardOrderResult.JOB_OPEN
    assert await guard.order(GuardOrder.PREPARE) is GuardOrderResult.PREPARING
    assert await guard.order(GuardOrder.PREPARE) is GuardOrderResult.WRONG_STATE
    # prepare dauert 6 s.
    ticks[0] = 5.9
    job = _details(guard).job
    assert job is not None
    assert job.state is GuardJobState.PREPARING
    ticks[0] = 6.0
    details = _details(guard)
    job = details.job
    assert job is not None
    assert job.state is GuardJobState.PREPARED
    assert (job.send_ready, job.blockers, job.confirm_reasons) == (True, (), ())
    for item in job.datasets:
        assert (item.mode, item.base_type) == ("incremental", "snapshot")
        assert (item.deleted, item.modified, item.added, item.renamed) == (12, 34, 56, 2)
        assert (item.referenced, item.base_referenced, item.size) == (
            2 * GIB,
            21 * GIB // 10,
            350 * MIB,
        )
    check = details.target_check
    assert check is not None
    assert check.result is GuardCheckResult.OK
    assert check.summary is not None
    assert (check.summary.available, check.summary.size, check.summary.reserve_percent) == (
        600 * GIB,
        860 * GIB,
        15,
    )
    assert [(m.name, m.state, m.count) for m in check.mirrors] == [
        (name, "mirror", 5) for name in SHARES
    ]
    assert await guard.order(GuardOrder.CLOSE) is GuardOrderResult.CLOSED
    job = _details(guard).job
    assert job is not None
    assert (job.state, job.result) == (GuardJobState.CLOSED, GuardJobResult.CLOSED)
    assert await guard.order(GuardOrder.CLOSE) is GuardOrderResult.NO_JOB
    # Lauf, Freigabe und Senden kommen erst mit v0.5.2.
    for order in (GuardOrder.BEGIN, GuardOrder.CONFIRM, GuardOrder.SEND):
        assert await guard.order(order) is GuardOrderResult.NOT_SUPPORTED


@pytest.mark.anyio
async def test_job_without_answer_times_out(tmp_path: Path) -> None:
    now, ticks = [NOW], [0.0]
    guard = _job_guard(tmp_path, now, ticks)
    await guard.order(GuardOrder.SNAPSHOT)
    now[0] = NOW + timedelta(minutes=30)
    job = _details(guard).job
    assert job is not None
    assert (job.state, job.result) == (GuardJobState.CLOSED, GuardJobResult.JOB_TIMEOUT)


@pytest.mark.parametrize(
    ("fault", "state", "reasons", "blockers"),
    [
        (
            "sync-deletions",
            GuardJobState.AWAITING_CONFIRM,
            (GuardConfirmReason.DELETIONS,),
            (),
        ),
        (
            "sync-empty-source",
            GuardJobState.AWAITING_CONFIRM,
            (GuardConfirmReason.SHRUNK,),
            (),
        ),
        ("sync-target-refused", GuardJobState.PREPARED, (), (GuardBlocker.SPACE_LOW,)),
        ("target-exists", GuardJobState.PREPARED, (), (GuardBlocker.TARGET_EXISTS,)),
        ("target-clone", GuardJobState.PREPARED, (), (GuardBlocker.CLONE_ACTIVE,)),
    ],
)
@pytest.mark.anyio
async def test_dry_run_faults(
    tmp_path: Path,
    fault: str,
    state: GuardJobState,
    reasons: tuple[GuardConfirmReason, ...],
    blockers: tuple[GuardBlocker, ...],
) -> None:
    set_flag(fault_flag(tmp_path, fault), True)
    ticks = [0.0]
    guard = _job_guard(tmp_path, [NOW], ticks)
    job = await _prepared(guard, ticks)
    assert (job.state, job.confirm_reasons, job.blockers) == (state, reasons, blockers)
    assert job.send_ready is (not blockers)
    check = _details(guard).target_check
    assert check is not None
    assert check.summary is not None
    if fault == "sync-deletions":
        assert [item.deleted for item in job.datasets] == [1500, 1500]
    elif fault == "sync-empty-source":
        assert [item.referenced for item in job.datasets] == [300 * MIB, 300 * MIB]
    elif fault == "sync-target-refused":
        assert check.summary.available == 20 * GIB
    elif fault == "target-exists":
        assert [item.problem for item in job.datasets] == [GuardDatasetProblem.TARGET_EXISTS] * 2
        assert [mirror.state for mirror in check.mirrors] == ["foreign", "foreign"]
    else:
        assert check.summary.clones == 1


@pytest.mark.anyio
async def test_inactive_guard_takes_no_orders(tmp_path: Path) -> None:
    _switch(tmp_path, "source-guard-inactive", NOW)
    guard = _job_guard(tmp_path, [NOW], [0.0])
    with pytest.raises(GuardOrderError) as caught:
        await guard.order(GuardOrder.SNAPSHOT)
    assert caught.value.outcome is ActionOutcome.SOURCE_GUARD_INACTIVE
    _switch(tmp_path, "source-guard-lost", NOW)
    fault_flag(tmp_path, "source-guard-inactive").unlink()
    with pytest.raises(GuardOrderError) as caught:
        await guard.order(GuardOrder.SNAPSHOT)
    assert caught.value.outcome is ActionOutcome.SOURCE_GUARD_LOST
