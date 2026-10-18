"""Aktionsbefehle im Handler: angenommen, abgelehnt mit Grund, Freischaltung (Konzept 4.2, 6.4)."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.passwords import password_check
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab
from twinraid.integrations.mocks.source_guard import MockSourceGuard
from twinraid.integrations.source_guard import GuardReport
from twinraid.shared.actions import ActionBlocker
from twinraid.shared.ipc import (
    ErrorCode,
    ErrorResponse,
    GetStatus,
    OkResponse,
    SyncStart,
    VmStart,
    WakeTarget,
)
from twinraid.shared.mode import RunMode
from twinraid.shared.source_guard import GuardState, SourceGuardStatus
from twinraid.shared.status import StatusNotice
from twinraid.worker.action_log import ActionLog
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.handler import CommandHandler
from twinraid.worker.monitor import StatusMonitor

pytestmark = pytest.mark.anyio
CONFIG = load_config(EXAMPLE_CONFIG)


async def _handler(
    tmp_path: Path, mode: RunMode = RunMode.MOCK, config: TwinRaidConfig = CONFIG
) -> tuple[CommandHandler, ActionCoordinator]:
    lab = MockLab(tmp_path)
    monitor = StatusMonitor(lab)
    await monitor.check_once()
    jobs = MockJobSwitch(tmp_path / "job")
    actions = ActionCoordinator(
        lab, monitor, jobs, config, mode, ActionLog(tmp_path / "actions.jsonl")
    )
    handler = CommandHandler(
        monitor, password_check(tmp_path, None), jobs, mode, actions, MockSourceGuard(tmp_path)
    )
    return handler, actions


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


@pytest.mark.parametrize("mode", [RunMode.MOCK, RunMode.REAL])
async def test_sync_stays_locked(tmp_path: Path, mode: RunMode) -> None:
    handler, _ = await _handler(tmp_path, mode)
    response = await handler.handle(SyncStart(command="sync_start"))
    assert isinstance(response, ErrorResponse)
    expected = ErrorCode.NOT_AVAILABLE if mode is RunMode.REAL else ErrorCode.NOT_IMPLEMENTED
    assert response.error is expected


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
