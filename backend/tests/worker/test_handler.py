"""Aktionsbefehle im Handler: angenommen, abgelehnt mit Grund, Freischaltung (Konzept 4.2, 6.4)."""

from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.passwords import password_check
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab
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
    handler = CommandHandler(monitor, password_check(tmp_path, None), jobs, mode, actions)
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
