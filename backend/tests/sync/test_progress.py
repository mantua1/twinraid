"""Fortschritt und Restzeit beim Senden (Konzept 4.4)."""

from datetime import UTC, datetime, timedelta

from twinraid.shared.source_guard import GuardDatasetResult, GuardJobDataset
from twinraid.sync.progress import ProgressTracker, current_share, sent_bytes, total_bytes

WALL = datetime(2026, 11, 6, 12, tzinfo=UTC)
MB = 1_000_000


def _share(
    name: str, size: int | None, sent: int | None, result: GuardDatasetResult | None = None
) -> GuardJobDataset:
    return GuardJobDataset(
        name=name,
        mode="incremental",
        base_type="snapshot",
        problem=None,
        deleted=0,
        modified=0,
        added=0,
        renamed=0,
        referenced=0,
        base_referenced=0,
        size=size,
        sent=sent,
        result=result,
    )


def _shares(first: int, second: int, first_done: bool = False) -> tuple[GuardJobDataset, ...]:
    return (
        _share("a", 100 * MB, first, GuardDatasetResult.OK if first_done else None),
        _share("b", 300 * MB, second),
    )


def test_sums() -> None:
    shares = (
        # Fertig: zählt mit der Größe, auch wenn sent kleiner gemeldet ist.
        _share("a", 100, 90, GuardDatasetResult.OK),
        _share("b", 50, 20),
        _share("c", None, None),
    )
    assert total_bytes(shares) == 150
    assert sent_bytes(shares) == 120
    assert current_share(shares) == "b"
    assert current_share((_share("a", 1, 1, GuardDatasetResult.OK),)) is None


def test_no_estimate_before_15_seconds() -> None:
    tracker = ProgressTracker()
    tracker.update(0.0, WALL, _shares(0, 0))
    tracker.update(10.0, WALL, _shares(50 * MB, 0))
    progress = tracker.snapshot()
    assert (progress.sent, progress.total, progress.percent) == (50 * MB, 400 * MB, 12)
    assert (progress.rate, progress.remaining_seconds, progress.finish_at) == (None, None, None)
    assert progress.share == "a"


def test_rate_remaining_and_end() -> None:
    tracker = ProgressTracker()
    for second in range(0, 17, 2):
        tracker.update(float(second), WALL, _shares(second * MB, 0))
    progress = tracker.snapshot()
    # 16 MB in 16 s; es fehlen noch 384 MB.
    assert progress.rate == MB
    assert progress.remaining_seconds == 384
    assert progress.finish_at == WALL + timedelta(seconds=384)


def test_recomputed_only_every_15_seconds() -> None:
    tracker = ProgressTracker()
    tracker.update(0.0, WALL, _shares(0, 0))
    tracker.update(15.0, WALL, _shares(15 * MB, 0))
    first = tracker.snapshot().rate
    tracker.update(20.0, WALL, _shares(50 * MB, 0))
    assert tracker.snapshot().rate == first
    tracker.update(30.0, WALL, _shares(60 * MB, 0))
    assert tracker.snapshot().rate == 60 * MB / 30


def test_moving_average_over_60_seconds() -> None:
    tracker = ProgressTracker()
    tracker.update(0.0, WALL, _shares(0, 0))
    # Am Anfang schnell, danach langsam: nur die letzten 60 s zählen.
    tracker.update(15.0, WALL, _shares(90 * MB, 0))
    for second in range(30, 106, 15):
        sent = 90 * MB + (second - 15) * MB // 10
        tracker.update(float(second), WALL, _shares(min(sent, 100 * MB), 0))
    rate = tracker.snapshot().rate
    assert rate is not None
    assert rate < 0.2 * MB


def test_stalled_transfer_has_no_end() -> None:
    tracker = ProgressTracker()
    tracker.update(0.0, WALL, _shares(10 * MB, 0))
    tracker.update(15.0, WALL, _shares(10 * MB, 0))
    progress = tracker.snapshot()
    assert progress.rate == 0
    assert (progress.remaining_seconds, progress.finish_at) == (None, None)


def test_at_most_99_percent_until_done() -> None:
    tracker = ProgressTracker()
    # Die Größe aus prepare ist eine Schätzung: gesendet kann mehr sein.
    tracker.update(0.0, WALL, (_share("a", 100, 150),))
    assert tracker.snapshot().percent == 99
    tracker.update(1.0, WALL, (_share("a", 100, 150, GuardDatasetResult.OK),), done=True)
    progress = tracker.snapshot()
    assert progress.percent == 100
    assert (progress.share, progress.remaining_seconds, progress.finish_at) == (None, None, None)


def test_empty_job() -> None:
    tracker = ProgressTracker()
    tracker.update(0.0, WALL, ())
    assert tracker.snapshot().percent == 0
