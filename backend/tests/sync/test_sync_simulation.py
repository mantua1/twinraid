"""Sync im Simulationsmodus (Konzept 6.4, ab v0.5.2)."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support.worker import TestWorker, make_worker
from twinraid.integrations.mocks.lab import LabTimings, fault_flag, set_flag
from twinraid.integrations.mocks.source_guard import MOCK_CONTAINER
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome
from twinraid.shared.source_guard import (
    GuardContainerState,
    GuardDatasetResult,
    GuardDetails,
    GuardJobReason,
)
from twinraid.shared.status import ResourceState
from twinraid.shared.sync import SyncStep

pytestmark = pytest.mark.anyio

FAST_LAB = LabTimings(
    boot_seconds=0.05,
    vm_service_seconds=0.05,
    vm_start_seconds=0.01,
    vm_stop_seconds=0.01,
    shutdown_seconds=0.01,
)


async def _worker(tmp_path: Path, ahead: timedelta = timedelta()) -> TestWorker:
    """Mit ahead geht die Uhr des Workers vor, wie nach dem Abstand zwischen Snapshots."""
    worker = make_worker(
        tmp_path, lab_timings=FAST_LAB, sync_clock=lambda: datetime.now(UTC) + ahead
    )
    await worker.lab.wake()
    for _ in range(200):
        status = await worker.monitor.check_once()
        if status.target.state is ResourceState.ONLINE and status.vm.reason is None:
            return worker
        await asyncio.sleep(0.01)
    raise AssertionError("simulated target did not come up")


async def _until(predicate: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _sync(worker: TestWorker) -> None:
    assert await worker.actions.request(ActionName.SYNC_START) is None
    await _until(lambda: worker.sync.snapshot().running is None)


def _last(worker: TestWorker) -> tuple[ActionOutcome, object]:
    last = worker.sync.snapshot().last
    assert last is not None
    return last.outcome, last.reason


def _guard_details(worker: TestWorker) -> GuardDetails:
    details = worker.handler.snapshot().source_guard.details
    assert details is not None
    return details


async def test_sync_in_the_simulation(tmp_path: Path) -> None:
    worker = await _worker(tmp_path)
    await _sync(worker)
    assert _last(worker) == (ActionOutcome.SUCCESS, None)
    details = _guard_details(worker)
    # Der Testcontainer läuft wieder; der Lauf ist zu, die Pause läuft.
    assert [(c.name, c.state) for c in details.allowed] == [
        (MOCK_CONTAINER, GuardContainerState.RUNNING)
    ]
    assert details.run is None
    assert details.last_run is not None
    assert details.run_allowed_at is not None
    history = worker.sync.snapshot().history
    assert history[0].containers_stopped == 1
    assert {share.result for share in history[0].shares} == {GuardDatasetResult.OK}
    # Die gehaltene Basis ist der gesendete Snapshot.
    job = details.job
    assert job is not None
    assert {dataset.base_created_at for dataset in details.zfs.datasets} == {job.created_at}
    # Die Pause (2 min) gilt nur für den Sync; der Abstand zwischen Snapshots (5 min, ab
    # v0.5.3) für beide und endet später, also nennt der Worker ihn (Konzept 4.2).
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON
    assert availability[ActionName.SYNC_DRY_RUN] is ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON
    allowed = worker.sync.snapshot().snapshot_allowed_at
    assert allowed is not None
    assert timedelta(minutes=5) <= allowed - job.created_at < timedelta(minutes=6)
    assert details.run_allowed_at < allowed


async def test_send_error_hits_the_last_share(tmp_path: Path) -> None:
    worker = await _worker(tmp_path)
    set_flag(fault_flag(tmp_path, "sync-send-error"), True)
    await _sync(worker)
    assert _last(worker) == (ActionOutcome.SEND_FAILED, GuardJobReason.UNREACHABLE)
    report = worker.sync.snapshot().report
    assert report is not None
    results = [share.result for share in report.shares]
    assert results[:-1] == [GuardDatasetResult.OK] * (len(results) - 1)
    assert results[-1] is GuardDatasetResult.SEND_FAILED


async def test_deletions_need_a_confirmation(tmp_path: Path) -> None:
    worker = await _worker(tmp_path)
    set_flag(fault_flag(tmp_path, "sync-deletions"), True)
    assert await worker.actions.request(ActionName.SYNC_START) is None

    def waiting() -> bool:
        running = worker.sync.snapshot().running
        return running is not None and running.step is SyncStep.AWAITING_CONFIRM

    await _until(waiting)
    running = worker.sync.snapshot().running
    assert running is not None
    assert running.confirmation is not None
    assert running.confirmation.deletions == 1500 * 3
    assert worker.sync.confirm(running.job_id) is True
    await _until(lambda: worker.sync.snapshot().running is None)
    assert _last(worker) == (ActionOutcome.SUCCESS, None)


async def test_slow_transfer_shows_progress_and_can_be_cancelled(tmp_path: Path) -> None:
    worker = await _worker(tmp_path)
    set_flag(fault_flag(tmp_path, "sync-slow"), True)
    assert await worker.actions.request(ActionName.SYNC_START) is None

    def sending() -> bool:
        running = worker.sync.snapshot().running
        return (
            running is not None
            and running.progress is not None
            and 0 < running.progress.percent < 100
        )

    await _until(sending)
    running = worker.sync.snapshot().running
    assert running is not None
    assert worker.sync.cancel(running.job_id) is True
    await _until(lambda: worker.sync.snapshot().running is None)
    assert _last(worker) == (ActionOutcome.CANCELLED, None)
    job = _guard_details(worker).job
    assert job is not None
    assert job.result is not None
    assert job.result.value == "closed"


async def test_clone_on_the_target_blocks_the_next_sync(tmp_path: Path) -> None:
    # Die Uhr des Workers geht 6 min vor: Der Abstand zwischen Snapshots sperrt nicht mehr.
    worker = await _worker(tmp_path, timedelta(minutes=6))
    set_flag(fault_flag(tmp_path, "target-clone"), True)
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    await _until(lambda: worker.sync.snapshot().running is None)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.TARGET_CLONE_ACTIVE
