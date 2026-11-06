"""Sync-Historie des Workers (Konzept 5.3, ab v0.5.2)."""

from datetime import UTC, datetime
from pathlib import Path

from twinraid.shared.actions import ActionOutcome, Trigger
from twinraid.shared.source_guard import GuardDatasetResult, GuardJobReason
from twinraid.shared.sync import SyncHistoryEntry, SyncHistoryShare, SyncKind
from twinraid.worker.sync_history import RECENT, SyncHistory, read_history

AT = datetime(2026, 11, 6, 12, tzinfo=UTC)


def _entry(number: int, kind: SyncKind = SyncKind.SYNC) -> SyncHistoryEntry:
    return SyncHistoryEntry(
        id=f"{number:08x}",
        kind=kind,
        trigger=Trigger.DASHBOARD,
        started_at=AT,
        finished_at=AT,
        result=ActionOutcome.SEND_FAILED,
        reason=GuardJobReason.UNREACHABLE,
        woke_target=True,
        containers_stopped=2,
        adopted=False,
        shares=(
            SyncHistoryShare(
                name="twinraidtest",
                mode="incremental",
                deleted=1,
                modified=2,
                added=3,
                renamed=4,
                size=1000,
                sent=500,
                result=GuardDatasetResult.SEND_FAILED,
            ),
        ),
    )


def test_append_and_read(tmp_path: Path) -> None:
    path = tmp_path / "sync-history.jsonl"
    history = SyncHistory(path)
    history.append(_entry(1, SyncKind.DRY_RUN))
    history.append(_entry(2))
    assert path.stat().st_mode & 0o777 == 0o600
    assert [entry.id for entry in read_history(path, 10)] == ["00000001", "00000002"]
    # Für get_status: neueste zuerst.
    assert [entry.id for entry in history.recent()] == ["00000002", "00000001"]
    assert read_history(path, 10)[1] == _entry(2)
    assert read_history(path, 0) == []


def test_recent_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "sync-history.jsonl"
    history = SyncHistory(path)
    for number in range(RECENT + 5):
        history.append(_entry(number))
    again = SyncHistory(path)
    recent = again.recent()
    assert len(recent) == RECENT
    assert recent[0].id == f"{RECENT + 4:08x}"


def test_rotation(tmp_path: Path) -> None:
    path = tmp_path / "sync-history.jsonl"
    history = SyncHistory(path, max_bytes=600)
    for number in range(4):
        history.append(_entry(number))
    rotated = tmp_path / "sync-history.jsonl.1"
    assert rotated.exists()
    # Genau eine ältere Datei; gelesen wird aus beiden.
    assert not (tmp_path / "sync-history.jsonl.2").exists()
    ids = [entry.id for entry in read_history(path, 10)]
    assert ids[-1] == "00000003"
    assert ids == sorted(ids)


def test_broken_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "sync-history.jsonl"
    path.write_text('not json\n{"id":"x"}\n' + _entry(1).model_dump_json() + "\n")
    assert [entry.id for entry in read_history(path, 10)] == ["00000001"]


def test_write_error_does_not_stop_the_run(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "sync-history.jsonl"
    history = SyncHistory(path)
    history.append(_entry(1))
    assert [entry.id for entry in history.recent()] == ["00000001"]


def test_memory_only(tmp_path: Path) -> None:
    history = SyncHistory(None)
    history.append(_entry(1))
    assert len(history.recent()) == 1
    assert list(tmp_path.iterdir()) == []
