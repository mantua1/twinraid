"""Probelauf ohne Wartezeit (Konzept 3.6, „Vorläufige Snapshots“ und „Protokoll 4“, v0.5.5).

Mit Protokoll 4 rechnet der Wächter den Abstand ab dem letzten behaltenen Snapshot und die
Pause nach einem verworfenen Snapshot selbst; TwinRaid übernimmt beide Zeitpunkte. Die Pause
sperrt Sync und Probelauf sekundengenau und steht als letzte Sperre.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support.scripted_guard import ScriptedGuard
from tests.sync.test_snapshot_interval import _job
from tests.sync.test_sync_run import _config, _guard, _online, _worker
from twinraid.shared.actions import ActionBlocker, ActionName
from twinraid.sync.coordinator import probe_allowed_at, snapshot_allowed_at

pytestmark = pytest.mark.anyio

DAY = "2026-11-21T"


def _at(value: str) -> datetime:
    return datetime.fromisoformat(DAY + value + "+00:00")


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.mark.parametrize(
    ("allowed", "now", "expected"),
    [
        # Vom Wächter, auf die volle Minute aufgerundet wie bei Protokoll 3.
        ("12:30:10", "12:10:00", "12:31:00"),
        ("12:30:00", "12:10:00", "12:30:00"),
        ("12:30:10", "12:31:00", None),
        (None, "12:10:00", None),
    ],
)
def test_snapshot_allowed_at_from_protocol_4(
    allowed: str | None, now: str, expected: str | None
) -> None:
    # Der Job (ein verworfener Probelauf) zählt bei Protokoll 4 nicht.
    guard = ScriptedGuard(
        protocol=4,
        snapshot_allowed_at=None if allowed is None else _at(allowed),
        job=_job(_at("12:09:00")),
    )
    result = snapshot_allowed_at(guard.details(), _at(now))
    assert result == (None if expected is None else _at(expected))


def test_snapshot_allowed_at_of_protocol_3_still_uses_the_job() -> None:
    guard = ScriptedGuard(job=_job(_at("12:00:10")), snapshot_allowed_at=_at("13:00:00"))
    assert snapshot_allowed_at(guard.details(), _at("12:10:00")) == _at("12:31:00")


@pytest.mark.parametrize(
    ("protocol", "allowed", "now", "expected"),
    [
        # Sekundengenau, ohne Aufrunden.
        (4, "12:00:08", "12:00:01", "12:00:08"),
        (4, "12:00:08", "12:00:08", None),
        (4, None, "12:00:01", None),
        # Protokoll 3 kennt die Pause nicht.
        (3, "12:00:08", "12:00:01", None),
    ],
)
def test_probe_allowed_at(
    protocol: int, allowed: str | None, now: str, expected: str | None
) -> None:
    guard = ScriptedGuard(
        protocol=protocol, probe_allowed_at=None if allowed is None else _at(allowed)
    )
    result = probe_allowed_at(guard.details(), _at(now))
    assert result == (None if expected is None else _at(expected))
    assert probe_allowed_at(None, _at(now)) is None


async def test_pause_blocks_sync_and_dry_run(tmp_path: Path) -> None:
    until = _now() + timedelta(seconds=8)
    worker = _worker(tmp_path, _guard(protocol=4, probe_allowed_at=until))
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.SOURCE_GUARD_PROBE_TOO_SOON
    assert availability[ActionName.SYNC_DRY_RUN] is ActionBlocker.SOURCE_GUARD_PROBE_TOO_SOON
    snapshot = worker.sync.snapshot()
    assert snapshot.probe_allowed_at == until
    # Ein verworfener Snapshot verschiebt den nächsten nicht.
    assert snapshot.snapshot_allowed_at is None


async def test_pause_is_the_last_reason(tmp_path: Path) -> None:
    """Ist die Pause der Grund, sperrt sonst nichts; die Oberfläche darf dann freigeben."""
    until = _now() + timedelta(seconds=8)
    guard = _guard(protocol=4, probe_allowed_at=until, shares=("twinraidtest", "andere"))
    worker = _worker(tmp_path, guard)
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.LEVELS_MISMATCH
    later = _now() + timedelta(minutes=20)
    (tmp_path / "second").mkdir()
    worker = _worker(
        tmp_path / "second",
        _guard(protocol=4, probe_allowed_at=until, snapshot_allowed_at=later),
    )
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.SOURCE_GUARD_SNAPSHOT_TOO_SOON


async def test_pause_over_releases(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard(protocol=4, probe_allowed_at=_now() - timedelta(seconds=1)))
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is None
    assert availability[ActionName.SYNC_DRY_RUN] is None
    assert worker.sync.snapshot().probe_allowed_at is None


async def test_sync_right_after_a_dry_run_without_containers(tmp_path: Path) -> None:
    """Mit stop_containers: false sperrt nach der Pause nichts mehr (keine Pause der Läufe)."""
    worker = _worker(
        tmp_path,
        _guard(protocol=4, probe_allowed_at=_now() - timedelta(seconds=1)),
        _config(stop_containers=False),
    )
    await _online(worker)
    assert worker.actions.availability(worker.monitor.current())[ActionName.SYNC_START] is None
