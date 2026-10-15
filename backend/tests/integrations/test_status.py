"""Statuslogik im Modus real, TCP-Check und Simulation (Konzept 3.5, 4.1 und 6.4)."""

import asyncio
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import load_config
from twinraid.integrations.control import RealControl
from twinraid.integrations.factory import build_integrations, build_job_state
from twinraid.integrations.jobs import NoJobs
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab
from twinraid.integrations.mocks.status import SCENARIO, STEP_SECONDS, MockStatusProvider
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.status import RealStatusProvider, map_vm_state
from twinraid.integrations.unraid_api import UnraidApiError
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.mode import RunMode
from twinraid.shared.status import ResourceInfo, ResourceState, StatusReason

pytestmark = pytest.mark.anyio
CONFIG = load_config(EXAMPLE_CONFIG)
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class FakeApi:
    def __init__(self, state: str | None = "RUNNING", error: StatusReason | None = None) -> None:
        self.state = state
        self.error = error
        self.calls: list[str] = []

    async def vm_state(self, vm_name: str) -> str | None:
        self.calls.append(vm_name)
        if self.error is not None:
            raise UnraidApiError(self.error, "test")
        return self.state


TARGET = "192.168.10.11"  # servers.target.host aus config.example.yaml
RUNS_HERE = ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.RUNS_HERE)


def _provider(
    api: FakeApi, target_up: bool, checked: list[str] | None = None
) -> RealStatusProvider:
    async def tcp(host: str, port: int) -> bool:
        assert port == 80  # webgui aus config.example.yaml
        if checked is not None:
            checked.append(host)
        assert host == TARGET, "only Unraid Groß is checked over TCP"
        return target_up

    return RealStatusProvider(CONFIG, api, tcp_check=tcp, clock=lambda: NOW)


async def test_everything_online() -> None:
    api = FakeApi("RUNNING")
    status = await _provider(api, target_up=True).get_status()
    assert status.source == RUNS_HERE
    assert status.target == ResourceInfo(state=ResourceState.ONLINE)
    assert status.vm == ResourceInfo(state=ResourceState.ONLINE)
    assert status.checked_at == NOW
    assert api.calls == ["Windows 11 Gaming"]


async def test_source_is_online_without_a_check() -> None:
    """TwinRaid läuft auf Klein: kein TCP-Check von Klein, auch wenn Groß offline ist."""
    checked: list[str] = []
    for target_up in (True, False):
        status = await _provider(FakeApi(), target_up, checked).get_status()
        assert status.source == RUNS_HERE
    assert checked == [TARGET, TARGET]


async def test_target_offline_means_vm_off_without_asking_the_api() -> None:
    api = FakeApi()
    status = await _provider(api, target_up=False).get_status()
    assert status.target == ResourceInfo(
        state=ResourceState.OFFLINE, reason=StatusReason.UNREACHABLE
    )
    assert status.vm == ResourceInfo(
        state=ResourceState.OFFLINE, reason=StatusReason.TARGET_OFFLINE
    )
    assert api.calls == []


@pytest.mark.parametrize(
    "reason",
    [
        StatusReason.API_UNREACHABLE,
        StatusReason.API_KEY_MISSING,
        StatusReason.API_KEY_INVALID,
        StatusReason.API_FORBIDDEN,
        StatusReason.API_CERT_UNTRUSTED,
        StatusReason.API_CERT_CHANGED,
        StatusReason.API_REDIRECTED,
        StatusReason.API_ERROR,
    ],
)
async def test_api_problems_are_distinguishable(reason: StatusReason) -> None:
    """Server online, aber die API scheitert: die Ursache steht beim VM-Status."""
    api = FakeApi(error=reason)
    status = await _provider(api, target_up=True).get_status()
    assert status.target.state is ResourceState.ONLINE
    assert status.vm == ResourceInfo(state=ResourceState.UNKNOWN, reason=reason)


async def test_vm_not_found() -> None:
    api = FakeApi(state=None)
    status = await _provider(api, target_up=True).get_status()
    assert status.vm == ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.VM_NOT_FOUND)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("RUNNING", ResourceInfo(state=ResourceState.ONLINE)),
        ("IDLE", ResourceInfo(state=ResourceState.ONLINE)),
        ("PAUSED", ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_PAUSED)),
        ("PMSUSPENDED", ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_SUSPENDED)),
        ("SHUTDOWN", ResourceInfo(state=ResourceState.STOPPING)),
        ("SHUTOFF", ResourceInfo(state=ResourceState.OFFLINE)),
        ("CRASHED", ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.VM_CRASHED)),
        ("NOSTATE", ResourceInfo(state=ResourceState.UNKNOWN)),
        ("IRGENDWAS", ResourceInfo(state=ResourceState.UNKNOWN)),
    ],
)
def test_vm_state_mapping(state: str, expected: ResourceInfo) -> None:
    assert map_vm_state(state) == expected


async def test_tcp_check_against_local_ports() -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    try:
        assert await tcp_reachable("127.0.0.1", port, wait_seconds=1) is True
    finally:
        listener.close()
    assert await tcp_reachable("127.0.0.1", port, wait_seconds=1) is False


async def test_tcp_check_times_out() -> None:
    # 192.0.2.0/24 ist TEST-NET-1 (RFC 5737) und wird nie geroutet.
    started = asyncio.get_running_loop().time()
    assert await tcp_reachable("192.0.2.1", 80, wait_seconds=0.2) is False
    assert asyncio.get_running_loop().time() - started < 2


async def test_mock_scenario_cycles_through_all_states() -> None:
    now = [0.0]
    provider = MockStatusProvider(monotonic=lambda: now[0], clock=lambda: NOW)
    seen_states: set[ResourceState] = set()
    seen_reasons: set[StatusReason] = set()
    for step in range(len(SCENARIO) + 1):
        now[0] = step * STEP_SECONDS
        status = await provider.get_status()
        assert status.checked_at == NOW
        for info in (status.source, status.target, status.vm):
            seen_states.add(info.state)
            if info.reason is not None:
                seen_reasons.add(info.reason)
    # Konzept 6.4: jeder Zustand und jede Ursache mindestens einmal.
    assert seen_states == set(ResourceState)
    assert seen_reasons == set(StatusReason)
    # Nach einem Durchlauf beginnt der Ablauf von vorn.
    assert provider.step() == 0


def test_mock_job_switch(tmp_path: Path) -> None:
    switch = MockJobSwitch(tmp_path / "mock-job-running")
    assert switch.is_running() is False
    switch.set_running(True)
    assert switch.is_running() is True
    assert (tmp_path / "mock-job-running").stat().st_mode & 0o777 == 0o600
    switch.set_running(False)
    switch.set_running(False)
    assert switch.is_running() is False


def test_factory_by_mode(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    mock = build_integrations(RunMode.MOCK, CONFIG, paths, tmp_path, None)
    assert isinstance(mock.status, MockLab)
    assert mock.control is mock.status
    real = build_integrations(RunMode.REAL, CONFIG, paths, tmp_path, None)
    assert isinstance(real.status, RealStatusProvider)
    assert isinstance(real.control, RealControl)
    # Den Rundlauf gibt es nur in der Simulation.
    (tmp_path / "mock-tour").touch()
    assert mock.tour_active() is True
    assert real.tour_active() is False
    assert isinstance(build_job_state(RunMode.MOCK, tmp_path / "flag"), MockJobSwitch)
    # Im Modus real läuft bis Phase 5 nie ein Kopierjob, auch wenn die Datei existiert.
    (tmp_path / "flag").touch()
    real_jobs = build_job_state(RunMode.REAL, tmp_path / "flag")
    assert isinstance(real_jobs, NoJobs)
    assert real_jobs.is_running() is False
