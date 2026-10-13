"""Statusprüfung im Worker und Antwort auf get_status (Konzept 4.1)."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.support.worker import make_handler
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.shared.ipc import GetStatus, OkResponse
from twinraid.shared.status import (
    NOT_CHECKED,
    ResourceInfo,
    ResourceState,
    ResourceStatus,
    StatusReason,
)
from twinraid.worker.monitor import StatusMonitor

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ONLINE = ResourceInfo(state=ResourceState.ONLINE)


class SequenceProvider:
    def __init__(self, *results: ResourceStatus | Exception) -> None:
        self._results = list(results)

    async def get_status(self) -> ResourceStatus:
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _status(vm: ResourceInfo) -> ResourceStatus:
    return ResourceStatus(source=ONLINE, target=ONLINE, vm=vm, checked_at=NOW)


async def test_monitor_keeps_the_last_result() -> None:
    first = _status(ONLINE)
    monitor = StatusMonitor(SequenceProvider(first), clock=lambda: NOW)
    assert monitor.current() == NOT_CHECKED
    await monitor.check_once()
    assert monitor.current() == first


async def test_failed_check_does_not_stop_the_worker(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StatusMonitor(SequenceProvider(RuntimeError("kaputt")), clock=lambda: NOW)
    status = await monitor.check_once()
    assert status.vm.state is ResourceState.UNKNOWN
    assert status.checked_at == NOW
    assert "status check failed" in caplog.text


async def test_changes_are_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    invalid = ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.API_KEY_INVALID)
    monitor = StatusMonitor(
        SequenceProvider(_status(ONLINE), _status(invalid), _status(invalid)), clock=lambda: NOW
    )
    with caplog.at_level(logging.INFO, logger="twinraid.worker.monitor"):
        for _ in range(3):
            await monitor.check_once()
    vm_lines = [r.getMessage() for r in caplog.records if "status vm" in r.getMessage()]
    assert vm_lines == [
        "status vm: unknown -> online",
        "status vm: online -> unknown (api_key_invalid)",
    ]


async def test_get_status_combines_monitor_job_and_password(tmp_path: Path) -> None:
    monitor = StatusMonitor(SequenceProvider(_status(ONLINE)), clock=lambda: NOW)
    await monitor.check_once()
    switch = MockJobSwitch(tmp_path / "flag")
    handler = make_handler(tmp_path, status=monitor, jobs=switch)
    response = await handler.handle(GetStatus(command="get_status"))
    assert isinstance(response, OkResponse)
    assert isinstance(response.result, dict)
    assert response.result["vm"] == {"state": "online", "reason": None}
    assert response.result["job_running"] is False
    assert response.result["password_set"] is True
    switch.set_running(True)
    response = await handler.handle(GetStatus(command="get_status"))
    assert isinstance(response, OkResponse)
    assert isinstance(response.result, dict)
    assert response.result["job_running"] is True
