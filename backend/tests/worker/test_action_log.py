"""Aktionslog des Workers (Konzept 5.3)."""

import json
import logging
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from twinraid.shared.actions import ActionName, ActionOutcome, Trigger
from twinraid.worker.action_log import (
    STARTED,
    ActionLog,
    new_action_id,
    read_entries,
    rotated,
)

NOW = datetime(2026, 10, 15, 18, 30, 5, tzinfo=UTC)


def _log(tmp_path: Path, max_bytes: int = 1024 * 1024) -> ActionLog:
    return ActionLog(tmp_path / "actions.jsonl", clock=lambda: NOW, max_bytes=max_bytes)


def test_entries_have_fixed_fields(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record("0a1b2c3d", ActionName.VM_START, Trigger.DASHBOARD, STARTED)
    log.record("0a1b2c3d", ActionName.WAKE_TARGET, Trigger.AUTOMATIC, STARTED, ActionName.VM_START)
    log.record("0a1b2c3d", ActionName.VM_START, Trigger.DASHBOARD, ActionOutcome.API_FORBIDDEN)
    lines = (tmp_path / "actions.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {
            "at": "2026-10-15T18:30:05Z",
            "id": "0a1b2c3d",
            "action": "vm_start",
            "trigger": "dashboard",
            "result": "started",
        },
        {
            "at": "2026-10-15T18:30:05Z",
            "id": "0a1b2c3d",
            "action": "wake_target",
            "trigger": "automatic",
            "result": "started",
            "part_of": "vm_start",
        },
        {
            "at": "2026-10-15T18:30:05Z",
            "id": "0a1b2c3d",
            "action": "vm_start",
            "trigger": "dashboard",
            "result": "api_forbidden",
        },
    ]


def test_file_is_private(tmp_path: Path) -> None:
    _log(tmp_path).record("1", ActionName.VM_STOP, Trigger.DASHBOARD, STARTED)
    assert stat.S_IMODE((tmp_path / "actions.jsonl").stat().st_mode) == 0o600


def test_entries_go_to_the_container_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="twinraid.worker.action_log"):
        _log(tmp_path).record(
            "ab",
            ActionName.WAKE_TARGET,
            Trigger.AUTOMATIC,
            ActionOutcome.SUCCESS,
            ActionName.VM_START,
        )
    assert "action ab wake_target by automatic (part of vm_start): success" in caplog.text


def test_rotates_at_the_size_limit(tmp_path: Path) -> None:
    log = _log(tmp_path, max_bytes=300)
    for number in range(10):
        log.record(f"{number:08x}", ActionName.VM_STOP, Trigger.DASHBOARD, STARTED)
    current = tmp_path / "actions.jsonl"
    assert rotated(current).exists()
    assert current.stat().st_size < 300 + 200
    # Es gibt genau eine ältere Datei.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["actions.jsonl", "actions.jsonl.1"]
    ids = [entry["id"] for entry in read_entries(current, 100)]
    assert ids == sorted(ids)
    assert ids[-1] == f"{9:08x}"


def test_read_entries_limits_and_skips_broken_lines(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    log = _log(tmp_path)
    for number in range(5):
        log.record(str(number), ActionName.VM_STOP, Trigger.DASHBOARD, STARTED)
    with path.open("a") as handle:
        handle.write("kaputt\n")
    assert [e["id"] for e in read_entries(path, 2)] == ["3", "4"]
    assert read_entries(path, 0) == []
    assert read_entries(tmp_path / "missing.jsonl", 10) == []


def test_symlinks_are_not_followed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    (tmp_path / "actions.jsonl").symlink_to(target)
    with caplog.at_level(logging.ERROR):
        _log(tmp_path).record("1", ActionName.VM_STOP, Trigger.DASHBOARD, STARTED)
    assert target.read_text() == ""
    assert "could not write the action log" in caplog.text


def test_action_ids_are_short_hex() -> None:
    first, second = new_action_id(), new_action_id()
    assert len(first) == 8
    assert int(first, 16) >= 0
    assert first != second
