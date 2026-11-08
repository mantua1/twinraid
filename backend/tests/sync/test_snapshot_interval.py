"""Abstand zwischen Snapshots, Dialog vor dem Sync und Historie (Konzept 3.6, 4.1, ab v0.5.3)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.support.scripted_guard import ScriptedGuard
from tests.sync.test_sync_run import (
    CONFIG,
    _config,
    _containers,
    _guard,
    _online,
    _sync,
    _worker,
)
from twinraid.shared.actions import ActionBlocker, ActionName
from twinraid.shared.source_guard import (
    GuardContainerState,
    GuardJob,
    GuardJobResult,
    GuardJobState,
    GuardState,
)
from twinraid.sync.coordinator import snapshot_allowed_at
from twinraid.worker.sync_history import read_history

pytestmark = pytest.mark.anyio


def _job(created_at: datetime) -> GuardJob:
    """Ein abgeschlossener Job; er bleibt im Status, bis der nächste Snapshot ihn ersetzt."""
    return GuardJob(
        id=7,
        state=GuardJobState.CLOSED,
        result=GuardJobResult.CLOSED,
        reason=None,
        snapshot="twinraid-20261108T120000Z",
        created_at=created_at,
        deadline=None,
        confirmed=False,
        confirm_reasons=(),
        send_ready=None,
        blockers=(),
        datasets=(),
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# --- frühester nächster Snapshot -------------------------------------------------------------


@pytest.mark.parametrize(
    ("created", "interval", "now", "expected"),
    [
        # Auf die volle Minute aufgerundet; eine volle Minute bleibt.
        ("12:00:10", 30, "12:10:00", "12:31:00"),
        ("12:00:00", 30, "12:10:00", "12:30:00"),
        # Abstand aus limits (Protokoll 3), etwa ein Testwert.
        ("12:00:10", 5, "12:02:00", "12:06:00"),
        # Bis zur angezeigten Minute gilt die Sperre, danach nicht mehr.
        ("12:00:10", 30, "12:30:59", "12:31:00"),
        ("12:00:10", 30, "12:31:00", None),
        # created_at in der Zukunft (Uhr von Klein gesprungen): nichts zeigen, nicht sperren.
        ("12:20:00", 30, "12:10:00", None),
    ],
)
def test_snapshot_allowed_at(created: str, interval: int, now: str, expected: str | None) -> None:
    day = "2026-11-08T"
    guard = ScriptedGuard(
        snapshot_interval=interval, job=_job(datetime.fromisoformat(day + created + "+00:00"))
    )
    allowed = snapshot_allowed_at(guard.details(), datetime.fromisoformat(day + now + "+00:00"))
    assert allowed == (
        None if expected is None else datetime.fromisoformat(day + expected + "+00:00")
    )


def test_no_job_no_limit() -> None:
    assert snapshot_allowed_at(ScriptedGuard().details(), _now()) is None
    assert snapshot_allowed_at(None, _now()) is None


# --- Sperre ----------------------------------------------------------------------------------


async def test_snapshot_interval_blocks_sync_and_dry_run(tmp_path: Path) -> None:
    created = _now()
    worker = _worker(tmp_path, _guard(job=_job(created)))
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON
    assert availability[ActionName.SYNC_DRY_RUN] is ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON
    allowed = worker.sync.snapshot().snapshot_allowed_at
    assert allowed is not None
    assert allowed.second == 0
    assert timedelta(minutes=30) <= allowed - created < timedelta(minutes=31)


async def test_snapshot_interval_has_passed(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard(job=_job(_now() - timedelta(minutes=6)), snapshot_interval=5))
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is None
    assert availability[ActionName.SYNC_DRY_RUN] is None
    assert worker.sync.snapshot().snapshot_allowed_at is None


@pytest.mark.parametrize(
    ("run_in", "blocker"),
    [
        # Gelten beide Grenzen, nennt der Worker die mit der späteren Uhrzeit (Konzept 4.3).
        (timedelta(minutes=10), ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON),
        (timedelta(minutes=45), ActionBlocker.SOURCE_GUARD_TOO_SOON),
    ],
)
async def test_the_later_limit_wins(
    tmp_path: Path, run_in: timedelta, blocker: ActionBlocker
) -> None:
    guard = _guard(job=_job(_now()), run_allowed_at=_now() + run_in)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is blocker
    # Den Probelauf betrifft nur der Abstand zwischen Snapshots.
    assert availability[ActionName.SYNC_DRY_RUN] is ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON


async def test_a_job_from_the_future_does_not_block(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard(job=_job(_now() + timedelta(hours=1))))
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is None
    assert worker.sync.snapshot().snapshot_allowed_at is None


async def test_inactive_guard_goes_first(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard(job=_job(_now()), state=GuardState.INACTIVE))
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.SOURCE_GUARD_INACTIVE
    # Ohne frischen Status gibt es keinen Job und also auch keine Uhrzeit.
    assert worker.sync.snapshot().snapshot_allowed_at is None


async def test_dry_run_after_a_sync_waits_for_the_interval(tmp_path: Path) -> None:
    guard = _guard()
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_DRY_RUN] is ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is (
        ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON
    )


# --- Dialog vor dem Sync ---------------------------------------------------------------------


async def test_preview_lists_the_candidates(tmp_path: Path) -> None:
    guard = _guard(
        allowed=(
            *_containers("web", "cache", "self"),
            *_containers("old", state=GuardContainerState.STOPPED),
        ),
        own=frozenset({"self"}),
    )
    worker = _worker(tmp_path, guard, _config(never_stop=["cache"]))
    await _online(worker)
    preview = worker.sync.snapshot().preview
    assert preview.containers == ("web",)
    assert preview.wake is False


async def test_preview_without_container_phase(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard(), _config(stop_containers=False))
    await _online(worker)
    assert worker.sync.snapshot().preview.containers is None


@pytest.mark.parametrize("values", [{"state": GuardState.INACTIVE}, {"state": GuardState.ERROR}])
async def test_preview_without_active_guard(tmp_path: Path, values: dict[str, Any]) -> None:
    worker = _worker(tmp_path, _guard(**values))
    await _online(worker)
    assert worker.sync.snapshot().preview.containers == ()


async def test_preview_wakes_an_offline_target(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard())
    await worker.monitor.check_once()
    assert worker.sync.snapshot().preview.wake is True
    await _online(worker)
    assert worker.sync.snapshot().preview.wake is False


async def test_preview_follows_the_status(tmp_path: Path) -> None:
    """Voraussichtlich: Der Sync bestimmt die Container beim Start neu (Konzept 4.1)."""
    guard = _guard()
    worker = _worker(tmp_path, guard, CONFIG)
    await _online(worker)
    assert worker.sync.snapshot().preview.containers == ("web", "db")
    guard.allowed = _containers("web", state=GuardContainerState.STOPPED)
    assert worker.sync.snapshot().preview.containers == ()


# --- Historie --------------------------------------------------------------------------------


async def test_history_has_the_base_type(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard())
    await _online(worker)
    await _sync(worker)
    entry = read_history(tmp_path / "sync-history.jsonl", 1)[0]
    assert {share.base_type for share in entry.shares} == {"snapshot"}
