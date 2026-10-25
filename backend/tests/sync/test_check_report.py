"""Ausgabe von twinraid-admin sync-check (Konzept 5.5, ab v0.5.1)."""

import asyncio
from pathlib import Path

import pytest

from tests.support.worker import TestWorker, make_worker
from twinraid.integrations.mocks.lab import LabTimings, fault_flag, set_flag
from twinraid.shared.actions import ActionName
from twinraid.shared.source_guard import GuardError, GuardState, SourceGuardStatus
from twinraid.sync.check_report import sync_check_lines
from twinraid.worker.actions import ActionTimings

pytestmark = pytest.mark.anyio

FAST_LAB = LabTimings(boot_seconds=0.05, vm_service_seconds=0.05)


def _worker(tmp_path: Path) -> TestWorker:
    return make_worker(
        tmp_path, action_timings=ActionTimings(poll_seconds=0.01), lab_timings=FAST_LAB
    )


async def _dry_run(worker: TestWorker) -> None:
    await worker.monitor.check_once()
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    for _ in range(500):
        if worker.sync.snapshot().running is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("dry run did not end")


async def test_before_the_first_dry_run(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    await worker.monitor.check_once()
    lines = sync_check_lines(worker.handler.snapshot())
    assert lines[:3] == [
        "TwinRaid sync check (read only)",
        "Source guard: active, version 2",
        "Own dataset of TwinRaid: found (needed: appdata is mirrored)",
    ]
    # Die Spiegel auf Groß kennt TwinRaid erst aus einem check (Probelauf).
    assert "  appdata                  yes  yes  ?" in lines
    assert "  mirror.conf is unknown until a dry run has asked the target (check)." in lines
    assert "Target: no check yet; start a dry run." in lines
    assert lines[-1] == "ZFS cache on the target: unknown (target offline or no status line yet)"


async def test_after_a_dry_run(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    await _dry_run(worker)
    await worker.monitor.check_once()
    lines = sync_check_lines(worker.handler.snapshot())
    assert "  dokumente                yes  yes  yes" in lines
    assert not any("mirror.conf is unknown" in line for line in lines)
    target = next(line for line in lines if line.startswith("Target: last check "))
    assert target.endswith(": ok")
    assert (
        "  free 600.0 GiB of 860.0 GiB, reserve 15 %, root ok, mirror.conf ok, "
        "delegation ok, receive idle, clones 0"
    ) in lines
    mirror = next(line for line in lines if line.startswith("  medien: "))
    assert mirror.startswith("  medien: mirror, snapshots 5, newest received ")
    assert mirror.endswith(", props ok")
    assert "ZFS cache on the target: zfs_arc_max 3.2 GiB, c_max 3.2 GiB" in lines
    assert lines[-1].startswith("Last dry run: dry_run_success at ")


async def test_arc_without_a_limit(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    set_flag(fault_flag(tmp_path, "arc-unlimited"), True)
    await worker.lab.wake()
    for _ in range(300):
        if (await worker.monitor.check_once()).target_zfs is not None:
            break
        await asyncio.sleep(0.01)
    lines = sync_check_lines(worker.handler.snapshot())
    assert lines[-1] == (
        "ZFS cache on the target: zfs_arc_max not set in Unraid "
        "(Settings → Disk Settings → zfs_arc_max), c_max 15.0 GiB"
    )


async def test_guard_problems_are_named(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    snapshot = worker.handler.snapshot()
    inactive = snapshot.model_copy(
        update={"source_guard": SourceGuardStatus(state=GuardState.INACTIVE)}
    )
    lines = sync_check_lines(inactive)
    assert lines[1:3] == ["Source guard: inactive", "Own dataset of TwinRaid: unknown"]
    failed = snapshot.model_copy(
        update={
            "source_guard": SourceGuardStatus(state=GuardState.ERROR, error=GuardError.STATE_LOST)
        }
    )
    assert sync_check_lines(failed)[1] == "Source guard: error (state_lost)"
