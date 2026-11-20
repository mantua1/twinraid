"""Statusprüfung im Worker und Antwort auf get_status (Konzept 4.1)."""

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.support.jobs import FakeJobs
from tests.support.worker import make_handler
from twinraid.shared.ipc import GetStatus, OkResponse
from twinraid.shared.status import (
    NOT_CHECKED,
    ResourceInfo,
    ResourceState,
    ResourceStatus,
    StatusNotice,
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
    invalid = ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.SSH_AUTH_FAILED)
    monitor = StatusMonitor(
        SequenceProvider(_status(ONLINE), _status(invalid), _status(invalid)), clock=lambda: NOW
    )
    with caplog.at_level(logging.INFO, logger="twinraid.worker.monitor"):
        for _ in range(3):
            await monitor.check_once()
    vm_lines = [r.getMessage() for r in caplog.records if "status vm" in r.getMessage()]
    assert vm_lines == [
        "status vm: unknown -> online",
        "status vm: online -> unknown (ssh_auth_failed)",
    ]


async def test_get_status_combines_monitor_job_and_password(tmp_path: Path) -> None:
    monitor = StatusMonitor(SequenceProvider(_status(ONLINE)), clock=lambda: NOW)
    await monitor.check_once()
    switch = FakeJobs()
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


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def get_status(self) -> ResourceStatus:
        self.calls += 1
        return _status(ONLINE)


async def _wait_for(provider: CountingProvider, calls: int, seconds: float = 2.0) -> float:
    started = time.monotonic()
    while provider.calls < calls:
        if time.monotonic() - started > seconds:
            pytest.fail(f"only {provider.calls} checks")
        await asyncio.sleep(0.01)
    return time.monotonic() - started


async def test_a_reported_change_triggers_a_check_at_once() -> None:
    """vm-watch meldet eine Änderung: nicht bis zur nächsten regulären Prüfung warten."""
    provider = CountingProvider()
    changes = asyncio.Event()
    monitor = StatusMonitor(provider, interval=60, clock=lambda: NOW, changes=changes)
    task = asyncio.create_task(monitor.run())
    try:
        await _wait_for(provider, 1)
        changes.set()
        assert await _wait_for(provider, 2) < 1
        # Danach wieder Ruhe bis zur nächsten Meldung.
        await asyncio.sleep(0.2)
        assert provider.calls == 2
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_without_changes_the_interval_applies() -> None:
    provider = CountingProvider()
    monitor = StatusMonitor(provider, interval=0.05, clock=lambda: NOW, changes=asyncio.Event())
    task = asyncio.create_task(monitor.run())
    try:
        await _wait_for(provider, 3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_notice_changes_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    with_notice = _status(ONLINE).model_copy(
        update={"notices": (StatusNotice.TARGET_CONNECT_PLUGIN,)}
    )
    monitor = StatusMonitor(
        SequenceProvider(_status(ONLINE), with_notice, with_notice, _status(ONLINE)),
        clock=lambda: NOW,
    )
    with caplog.at_level(logging.INFO, logger="twinraid.worker.monitor"):
        for _ in range(4):
            await monitor.check_once()
    lines = [r.getMessage() for r in caplog.records if "notices" in r.getMessage()]
    assert lines == ["status notices: target_connect_plugin", "status notices: none"]


# --- Rechte der Konfiguration (Konzept 6.1, ab v0.5.4) ---


async def test_monitor_adds_config_writable(tmp_path: Path) -> None:
    seen: list[Path] = []

    def unprotected(path: Path) -> bool:
        seen.append(path)
        return True

    config = tmp_path / "twinraid.yaml"
    monitor = StatusMonitor(
        SequenceProvider(_status(ONLINE)),
        clock=lambda: NOW,
        config_path=config,
        unprotected=unprotected,
    )
    status = await monitor.check_once()
    assert status.notices == (StatusNotice.CONFIG_WRITABLE,)
    assert seen == [config]


async def test_monitor_without_config_notice_when_protected(tmp_path: Path) -> None:
    monitor = StatusMonitor(
        SequenceProvider(_status(ONLINE)),
        clock=lambda: NOW,
        config_path=tmp_path / "twinraid.yaml",
        unprotected=lambda _path: False,
    )
    assert (await monitor.check_once()).notices == ()


async def test_monitor_checks_the_config_again_each_time(tmp_path: Path) -> None:
    # Ein chmod wirkt ohne Neustart: bei der nächsten Prüfung ist der Hinweis weg.
    answers = [True, False]
    monitor = StatusMonitor(
        SequenceProvider(_status(ONLINE), _status(ONLINE)),
        clock=lambda: NOW,
        config_path=tmp_path / "twinraid.yaml",
        unprotected=lambda _path: answers.pop(0),
    )
    assert StatusNotice.CONFIG_WRITABLE in (await monitor.check_once()).notices
    assert StatusNotice.CONFIG_WRITABLE not in (await monitor.check_once()).notices


async def test_monitor_adds_the_config_notice_once_and_after_a_failed_check(
    tmp_path: Path,
) -> None:
    # Der Rundlauf der Simulation bringt den Hinweis selbst mit; er erscheint nicht doppelt.
    tour = ResourceStatus(
        source=ONLINE,
        target=ONLINE,
        vm=ONLINE,
        checked_at=NOW,
        notices=(StatusNotice.CONFIG_WRITABLE,),
    )
    monitor = StatusMonitor(
        SequenceProvider(tour, RuntimeError("kaputt")),
        clock=lambda: NOW,
        config_path=tmp_path / "twinraid.yaml",
        unprotected=lambda _path: True,
    )
    assert (await monitor.check_once()).notices == (StatusNotice.CONFIG_WRITABLE,)
    assert (await monitor.check_once()).notices == (StatusNotice.CONFIG_WRITABLE,)
