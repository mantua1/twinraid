"""Aktionsbefehle im Handler: angenommen, abgelehnt mit Grund, Freischaltung (Konzept 4.2, 6.4)."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.worker import make_worker
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.source_guard import GuardReport
from twinraid.shared.actions import ActionBlocker
from twinraid.shared.ipc import (
    ErrorCode,
    ErrorResponse,
    GetStatus,
    OkResponse,
    SyncCancel,
    SyncConfirmDeletions,
    SyncStart,
    VmStart,
    WakeTarget,
)
from twinraid.shared.mode import RunMode
from twinraid.shared.source_guard import GuardState, SourceGuardStatus
from twinraid.shared.status import StatusNotice
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.handler import CommandHandler

pytestmark = pytest.mark.anyio
CONFIG = load_config(EXAMPLE_CONFIG)


async def _handler(
    tmp_path: Path, mode: RunMode = RunMode.MOCK, config: TwinRaidConfig = CONFIG
) -> tuple[CommandHandler, ActionCoordinator]:
    worker = make_worker(tmp_path, mode, password=None, config=config)
    await worker.monitor.check_once()
    return worker.handler, worker.actions


async def test_accepted_then_busy(tmp_path: Path) -> None:
    handler, actions = await _handler(tmp_path)
    response = await handler.handle(WakeTarget(command="wake_target"))
    assert response == OkResponse(result={"accepted": True})
    again = await handler.handle(WakeTarget(command="wake_target"))
    assert isinstance(again, ErrorResponse)
    assert again.error is ErrorCode.ACTION_BLOCKED
    assert again.blocked_by is ActionBlocker.BUSY
    status = await handler.handle(GetStatus(command="get_status"))
    assert isinstance(status, OkResponse)
    assert isinstance(status.result, dict)
    assert status.result["target"] == {"state": "starting", "reason": None}
    await actions.close()


async def test_real_mode_without_switches(tmp_path: Path) -> None:
    handler, _ = await _handler(tmp_path, RunMode.REAL)
    for command in (WakeTarget(command="wake_target"), VmStart(command="vm_start")):
        response = await handler.handle(command)
        assert isinstance(response, ErrorResponse)
        assert response.error is ErrorCode.NOT_AVAILABLE
        assert response.blocked_by is ActionBlocker.NOT_ENABLED


async def test_real_mode_with_wake_enabled(tmp_path: Path) -> None:
    config = CONFIG.model_copy(update={"actions": CONFIG.actions.model_copy(update={"wake": True})})
    handler, actions = await _handler(tmp_path, RunMode.REAL, config)
    assert await handler.handle(WakeTarget(command="wake_target")) == OkResponse(
        result={"accepted": True}
    )
    await actions.close()


async def test_sync_start_is_accepted(tmp_path: Path) -> None:
    """Ab v0.5.2 gibt es den Sync (Konzept 4.3); in der Simulation darf er Groß wecken."""
    worker = make_worker(tmp_path, password=None)
    await worker.monitor.check_once()
    response = await worker.handler.handle(SyncStart(command="sync_start"))
    assert response == OkResponse(result={"accepted": True})
    await worker.close()


async def test_sync_start_in_real_mode(tmp_path: Path) -> None:
    handler, _ = await _handler(tmp_path, RunMode.REAL)
    response = await handler.handle(SyncStart(command="sync_start"))
    assert isinstance(response, ErrorResponse)
    # Zuerst die Freischaltung (actions.sync ist aus).
    assert response.error is ErrorCode.NOT_AVAILABLE
    assert response.blocked_by is ActionBlocker.NOT_ENABLED
    config = CONFIG.model_copy(update={"actions": CONFIG.actions.model_copy(update={"sync": True})})
    (tmp_path / "enabled").mkdir()
    handler, _ = await _handler(tmp_path / "enabled", RunMode.REAL, config)
    response = await handler.handle(SyncStart(command="sync_start"))
    assert isinstance(response, ErrorResponse)
    # Groß ist aus, und Wecken ist nicht freigeschaltet.
    assert response.blocked_by is ActionBlocker.TARGET_OFFLINE


async def test_confirm_without_a_waiting_sync(tmp_path: Path) -> None:
    handler, _ = await _handler(tmp_path)
    confirm = await handler.handle(
        SyncConfirmDeletions(command="sync_confirm_deletions", job_id="a" * 32)
    )
    assert isinstance(confirm, ErrorResponse)
    assert confirm.error is ErrorCode.ACTION_BLOCKED
    assert confirm.blocked_by is None


async def test_cancel_without_a_running_dry_run(tmp_path: Path) -> None:
    handler, _ = await _handler(tmp_path)
    response = await handler.handle(SyncCancel(command="sync_cancel", job_id="b" * 32))
    assert isinstance(response, ErrorResponse)
    assert response.error is ErrorCode.ACTION_BLOCKED
    assert response.blocked_by is None


class _Guard:
    """Wächter mit festem Bericht; zählt, wie oft get_status ihn auswertet."""

    def __init__(self, report: GuardReport) -> None:
        self.calls = 0
        self._report = report

    def report(self) -> GuardReport:
        self.calls += 1
        return self._report


async def test_status_carries_the_source_guard(tmp_path: Path) -> None:
    handler, _actions = await _handler(tmp_path)
    guard = _Guard(
        GuardReport(
            SourceGuardStatus(state=GuardState.INACTIVE),
            (StatusNotice.SOURCE_CONNECT_PLUGIN,),
        )
    )
    handler._source_guard = guard
    (tmp_path / "mock-fault-connect-plugin").touch()
    snapshot = handler.snapshot()
    assert snapshot.source_guard.state is GuardState.INACTIVE
    # Die Hinweise des Wächters kommen zu denen der Kacheln Groß und VM hinzu.
    assert StatusNotice.SOURCE_CONNECT_PLUGIN in snapshot.notices
    # Ausgewertet wird bei jedem get_status, damit das Alter des Herzschlags stimmt.
    handler.snapshot()
    assert guard.calls == 2
    response = await handler.handle(GetStatus(command="get_status"))
    assert isinstance(response, OkResponse)
    assert isinstance(response.result, dict)
    guard_status = response.result["source_guard"]
    assert isinstance(guard_status, dict)
    assert guard_status["state"] == "inactive"
