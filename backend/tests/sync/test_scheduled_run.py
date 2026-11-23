"""Geplanter Lauf mit dem simulierten Wächter (Konzept 4.3, 4.8 und 4.2, ab v0.6.0)."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support.worker import TestWorker, make_worker
from twinraid.integrations.mocks.lab import LabTimings, fault_flag, set_flag
from twinraid.shared.actions import ActionName, ActionOutcome, Trigger
from twinraid.shared.ipc import AutoShutdownCancel, SyncStart, VmStart
from twinraid.shared.schedule import AutoShutdownKind
from twinraid.shared.status import ResourceState
from twinraid.shared.sync import SyncKind

pytestmark = pytest.mark.anyio

FAST_LAB = LabTimings(
    boot_seconds=0.05,
    vm_service_seconds=0.05,
    vm_start_seconds=0.01,
    vm_stop_seconds=0.01,
    shutdown_seconds=0.01,
)
# Sonntag, 29.11.2026, 03:00 in Europe/Berlin (Beispielkonfiguration).
TERM = datetime(2026, 11, 29, 2, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = TERM - timedelta(hours=1)

    def __call__(self) -> datetime:
        return self.now


async def _until(predicate: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _online(worker: TestWorker) -> None:
    await worker.lab.wake()
    for _ in range(200):
        status = await worker.monitor.check_once()
        if status.target.state is ResourceState.ONLINE and status.vm.reason is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("simulated target did not come up")


async def _refresh(worker: TestWorker) -> None:
    await worker.monitor.check_once()


def _log(directory: Path) -> list[tuple[str, str, str, str | None]]:
    lines = (directory / "actions.jsonl").read_text().splitlines()
    entries = [json.loads(line) for line in lines]
    return [(e["action"], e["trigger"], e["result"], e.get("part_of")) for e in entries]


async def _scheduled(worker: TestWorker, clock: Clock) -> None:
    clock.now = TERM + timedelta(seconds=5)
    await worker.scheduler.tick()
    assert worker.sync.snapshot().running is not None
    running = worker.sync.snapshot().running
    assert running is not None
    assert (running.trigger, running.scheduled_for) == (Trigger.SCHEDULE, TERM)
    await _until(lambda: worker.sync.snapshot().running is None)


async def test_scheduled_run_until_done(tmp_path: Path) -> None:
    clock = Clock()
    worker = make_worker(tmp_path, lab_timings=FAST_LAB, schedule_clock=clock)
    await _online(worker)
    await _scheduled(worker, clock)
    entry = worker.sync.snapshot().history[0]
    assert (entry.kind, entry.trigger, entry.result, entry.scheduled_for, entry.woke_target) == (
        SyncKind.SYNC,
        Trigger.SCHEDULE,
        ActionOutcome.SUCCESS,
        TERM,
        False,
    )
    assert entry.shares
    assert ("sync_start", "schedule", "success", None) in _log(tmp_path)
    last = worker.scheduler.snapshot().last
    assert last is not None
    assert (last.scheduled_for, last.outcome) == (TERM, ActionOutcome.SUCCESS)
    # Groß war schon an: keine Vormerkung, Groß bleibt an.
    assert worker.handler.snapshot().auto_shutdown is None
    await worker.close()


async def test_scheduled_run_with_deletions_ends_with_confirmation_required(
    tmp_path: Path,
) -> None:
    clock = Clock()
    worker = make_worker(tmp_path, lab_timings=FAST_LAB, schedule_clock=clock)
    await _online(worker)
    before = worker.handler.snapshot().source_guard.details
    assert before is not None
    counts = {dataset.name: dataset.snapshots for dataset in before.zfs.datasets}
    set_flag(fault_flag(tmp_path, "sync-deletions"), True)
    await _scheduled(worker, clock)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is ActionOutcome.CONFIRMATION_REQUIRED
    # Die Gründe stehen im Bericht; der Wächter hat den vorläufigen Snapshot verworfen.
    report = worker.sync.snapshot().report
    assert report is not None
    assert report.awaiting_confirm is True
    after = worker.handler.snapshot().source_guard.details
    assert after is not None
    assert after.job is not None
    assert after.job.open is False
    assert {dataset.name: dataset.snapshots for dataset in after.zfs.datasets} == counts
    assert worker.sync.snapshot().history[0].result is ActionOutcome.CONFIRMATION_REQUIRED
    await worker.close()


async def test_scheduled_run_that_woke_the_target_shuts_it_down(tmp_path: Path) -> None:
    clock = Clock()
    worker = make_worker(tmp_path, lab_timings=FAST_LAB, schedule_clock=clock)
    await _refresh(worker)
    assert worker.monitor.current().target.state is ResourceState.OFFLINE
    await _scheduled(worker, clock)
    assert worker.sync.snapshot().history[0].woke_target is True
    assert worker.sync.snapshot().last is not None
    await _refresh(worker)
    worker.auto_shutdown.tick()
    auto = worker.handler.snapshot().auto_shutdown
    assert auto is not None
    assert auto.kinds == (AutoShutdownKind.AFTER_SYNC,)
    assert auto.at == clock.now + timedelta(minutes=5)
    clock.now += timedelta(minutes=5)
    await _refresh(worker)
    worker.auto_shutdown.tick()
    await _until(lambda: not worker.actions.busy)
    await _refresh(worker)
    assert worker.monitor.current().target.state is ResourceState.OFFLINE
    assert _log(tmp_path)[-2:] == [
        ("shutdown_target", "automatic", "started", "sync_start"),
        ("shutdown_target", "automatic", "success", "sync_start"),
    ]
    await worker.close()


async def test_manual_sync_drops_the_mark(tmp_path: Path) -> None:
    clock = Clock()
    # Die Uhr des Syncs geht vor, damit der Abstand zwischen Snapshots den zweiten Lauf
    # nicht schon im Worker sperrt.
    worker = make_worker(
        tmp_path,
        lab_timings=FAST_LAB,
        schedule_clock=clock,
        sync_clock=lambda: datetime.now(UTC) + timedelta(minutes=6),
    )
    await _refresh(worker)
    await _scheduled(worker, clock)
    await _refresh(worker)
    assert worker.auto_shutdown.offered()
    # Ein Sync von Hand ist eine manuelle Aktion (Konzept 4.2); er lässt Groß an.
    response = await worker.handler.handle(SyncStart(command="sync_start"))
    assert response.ok is True
    assert not worker.auto_shutdown.offered()
    await _until(lambda: worker.sync.snapshot().running is None)
    await worker.close()


async def test_manual_vm_start_drops_only_the_sync_mark(tmp_path: Path) -> None:
    clock = Clock()
    worker = make_worker(tmp_path, lab_timings=FAST_LAB, schedule_clock=clock)
    await _refresh(worker)
    await _scheduled(worker, clock)
    await _refresh(worker)
    assert worker.auto_shutdown.offered()
    response = await worker.handler.handle(VmStart(command="vm_start"))
    assert response.ok is True
    assert not worker.auto_shutdown.offered()
    await _until(lambda: not worker.actions.busy)
    await worker.close()


async def test_skipped_term_appears_in_the_status(tmp_path: Path) -> None:
    clock = Clock()
    worker = make_worker(tmp_path, lab_timings=FAST_LAB, schedule_clock=clock)
    await _online(worker)
    set_flag(fault_flag(tmp_path, "source-guard-inactive"), True)
    clock.now = TERM + timedelta(seconds=5)
    await worker.scheduler.tick()
    status = worker.handler.snapshot()
    assert status.schedule.waiting is not None
    clock.now = TERM + timedelta(minutes=61)
    await worker.scheduler.tick()
    status = worker.handler.snapshot()
    assert status.schedule.waiting is None
    assert status.schedule.last is not None
    assert status.schedule.last.outcome is ActionOutcome.SCHEDULE_SKIPPED
    assert status.sync.history[0].result is ActionOutcome.SCHEDULE_SKIPPED
    await worker.close()


async def test_auto_shutdown_cancel_through_the_handler(tmp_path: Path) -> None:
    clock = Clock()
    worker = make_worker(tmp_path, lab_timings=FAST_LAB, schedule_clock=clock)
    await _refresh(worker)
    await _scheduled(worker, clock)
    await _refresh(worker)
    snapshot = worker.handler.snapshot()
    assert snapshot.actions.availability[ActionName.AUTO_SHUTDOWN_CANCEL] is None
    response = await worker.handler.handle(AutoShutdownCancel(command="auto_shutdown_cancel"))
    assert response.ok is True
    assert worker.handler.snapshot().auto_shutdown is None
    response = await worker.handler.handle(AutoShutdownCancel(command="auto_shutdown_cancel"))
    assert response.ok is False
    await worker.close()
