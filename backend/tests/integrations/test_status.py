"""Statuslogik im Modus real, TCP-Check und Simulation (Konzept 3.5, 4.1 und 6.4)."""

import asyncio
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.watch import StubWatch, report
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.control import RealControl
from twinraid.integrations.factory import build_integrations
from twinraid.integrations.guard_orders import RealGuardChannel
from twinraid.integrations.mocks.lab import MockLab
from twinraid.integrations.mocks.source_guard import GUARD_SCENARIO, MockSourceGuard
from twinraid.integrations.mocks.status import (
    SCENARIO,
    STEP_SECONDS,
    TRANSIENT_REASONS,
    MockStatusProvider,
)
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.source_guard import RealSourceGuard
from twinraid.integrations.status import RealStatusProvider, map_vm_state
from twinraid.integrations.vm_watch import PERMANENT_REASONS, WatchReport, WatchView
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.mode import RunMode
from twinraid.shared.status import (
    ResourceInfo,
    ResourceState,
    StatusNotice,
    StatusReason,
    TargetZfs,
    WatchRetry,
)

pytestmark = pytest.mark.anyio
CONFIG = load_config(EXAMPLE_CONFIG)
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
TARGET = "192.168.10.11"  # servers.target.host aus config.example.yaml
RUNS_HERE = ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.RUNS_HERE)


def _provider(
    watch: StubWatch,
    target_up: bool,
    checked: list[str] | None = None,
    config: TwinRaidConfig = CONFIG,
) -> RealStatusProvider:
    async def tcp(host: str, port: int) -> bool:
        assert port == 80  # webgui aus config.example.yaml
        if checked is not None:
            checked.append(host)
        assert host == TARGET, "only Unraid Groß is checked over TCP"
        return target_up

    return RealStatusProvider(config, watch, tcp_check=tcp, clock=lambda: NOW)


async def test_everything_online() -> None:
    watch = StubWatch()
    status = await _provider(watch, target_up=True).get_status()
    assert status.source == RUNS_HERE
    assert status.target == ResourceInfo(state=ResourceState.ONLINE)
    assert status.vm == ResourceInfo(state=ResourceState.ONLINE)
    assert status.checked_at == NOW
    assert status.other_vms_running == ()
    assert status.notices == ()
    # Die Sitzung erfährt jedes Ergebnis der TCP-Prüfung.
    assert watch.online_calls == [True]


async def test_next_attempt_comes_from_the_session() -> None:
    """Wartet die Sitzung nach einem Fehler, steht der nächste Versuch im Status (ab v0.4.4)."""
    retry = WatchRetry(at=NOW + timedelta(minutes=30), paused=True)
    watch = StubWatch(WatchView(None, StatusReason.SSH_AUTH_FAILED, retry))
    status = await _provider(watch, target_up=True).get_status()
    assert status.vm == ResourceInfo(
        state=ResourceState.UNKNOWN, reason=StatusReason.SSH_AUTH_FAILED
    )
    assert status.watch_retry == retry
    # Ist Groß aus, gibt es keine Sitzung und keinen Versuch.
    status = await _provider(watch, target_up=False).get_status()
    assert status.watch_retry is None
    # Mit einem Stand der Sitzung auch nicht.
    assert (await _provider(StubWatch(), target_up=True).get_status()).watch_retry is None


async def test_source_is_online_without_a_check() -> None:
    """TwinRaid läuft auf Klein: kein TCP-Check von Klein, auch wenn Groß offline ist."""
    checked: list[str] = []
    for target_up in (True, False):
        status = await _provider(StubWatch(), target_up, checked).get_status()
        assert status.source == RUNS_HERE
    assert checked == [TARGET, TARGET]


async def test_target_offline_means_vm_off_without_a_session() -> None:
    watch = StubWatch()
    status = await _provider(watch, target_up=False).get_status()
    assert status.target == ResourceInfo(
        state=ResourceState.OFFLINE, reason=StatusReason.UNREACHABLE
    )
    assert status.vm == ResourceInfo(
        state=ResourceState.OFFLINE, reason=StatusReason.TARGET_OFFLINE
    )
    assert status.other_vms_running is None
    assert status.notices == ()
    assert watch.online_calls == [False]


@pytest.mark.parametrize(
    "reason",
    [
        StatusReason.WATCH_CONNECTING,
        StatusReason.WATCH_INTERRUPTED,
        StatusReason.SSH_KEY_MISSING,
        StatusReason.SSH_HOST_UNTRUSTED,
        StatusReason.SSH_HOST_KEY_MISMATCH,
        StatusReason.SSH_AUTH_FAILED,
        StatusReason.SSH_UNREACHABLE,
        StatusReason.SSH_DENIED,
        StatusReason.SSH_FAILED,
        StatusReason.GUARD_OUTDATED,
    ],
)
async def test_session_problems_are_distinguishable(reason: StatusReason) -> None:
    """Server online, aber die Sitzung liefert keinen Stand: die Ursache steht beim VM-Status."""
    watch = StubWatch()
    watch.show(reason)
    status = await _provider(watch, target_up=True).get_status()
    assert status.target.state is ResourceState.ONLINE
    assert status.vm == ResourceInfo(state=ResourceState.UNKNOWN, reason=reason)
    # Ohne Stand ist auch unbekannt, ob weitere VMs laufen.
    assert status.other_vms_running is None


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("NOT_CONFIGURED", StatusReason.VM_NOT_CONFIGURED),
        ("NOT_FOUND", StatusReason.VM_NOT_FOUND),
        ("LIBVIRT_DOWN", StatusReason.VM_SERVICE_UNAVAILABLE),
        ("UNKNOWN", StatusReason.VM_SERVICE_UNAVAILABLE),
    ],
)
async def test_guard_reports_are_distinguishable(state: str, reason: StatusReason) -> None:
    watch = StubWatch()
    watch.show(report(state, others=None, name=None))
    status = await _provider(watch, target_up=True).get_status()
    assert status.vm == ResourceInfo(state=ResourceState.UNKNOWN, reason=reason)
    assert status.other_vms_running is None


async def test_vm_not_found_still_lists_other_vms() -> None:
    watch = StubWatch()
    watch.show(report("NOT_FOUND", others=("Andere VM",), name=None))
    status = await _provider(watch, target_up=True).get_status()
    assert status.vm == ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.VM_NOT_FOUND)
    assert status.other_vms_running == ("Andere VM",)


async def test_other_running_vms_come_from_the_session() -> None:
    watch = StubWatch()
    watch.show(report("SHUTOFF", others=("Abgestürzt", "Läuft")))
    status = await _provider(watch, target_up=True).get_status()
    assert status.other_vms_running == ("Abgestürzt", "Läuft")
    assert status.vm == ResourceInfo(state=ResourceState.OFFLINE)


@pytest.mark.parametrize(
    ("value", "notices"),
    [
        (report(connect_plugin=True), (StatusNotice.TARGET_CONNECT_PLUGIN,)),
        (report(flash_backup="active"), (StatusNotice.TARGET_FLASH_BACKUP,)),
        (
            report(connect_plugin=True, flash_backup="active"),
            (StatusNotice.TARGET_CONNECT_PLUGIN, StatusNotice.TARGET_FLASH_BACKUP),
        ),
        (report(flash_backup="unknown"), ()),
        (report(name="Andere VM"), (StatusNotice.VM_NAME_MISMATCH,)),
        (report(name=None), ()),
    ],
)
async def test_notices(value: WatchReport, notices: tuple[StatusNotice, ...]) -> None:
    watch = StubWatch()
    watch.show(value)
    status = await _provider(watch, target_up=True).get_status()
    assert status.notices == notices


@pytest.mark.parametrize(
    ("arc_max", "c_max", "notices"),
    [
        (4 * 1024**3, 4 * 1024**3, ()),
        # 0: In Unraid ist keine Grenze eingetragen (Konzept 3.5, „Grenze des ARC“).
        (0, 15 * 1024**3, (StatusNotice.TARGET_ARC_UNLIMITED,)),
        # „-“ (ZFS nicht geladen) und „?“ (nicht lesbar) ergeben keinen Hinweis.
        (None, None, ()),
    ],
)
async def test_arc_values_from_the_session(
    arc_max: int | None, c_max: int | None, notices: tuple[StatusNotice, ...]
) -> None:
    watch = StubWatch()
    watch.show(report(arc_max=arc_max, c_max=c_max))
    status = await _provider(watch, target_up=True).get_status()
    assert status.target_zfs == TargetZfs(arc_max=arc_max, c_max=c_max)
    assert status.notices == notices


async def test_no_arc_values_without_a_session() -> None:
    status = await _provider(StubWatch(), target_up=False).get_status()
    assert status.target_zfs is None


async def test_no_name_check_without_unraid_vm_name() -> None:
    config = CONFIG.model_copy(
        update={"gaming_vm": CONFIG.gaming_vm.model_copy(update={"unraid_vm_name": None})}
    )
    watch = StubWatch()
    watch.show(report(name="Irgendein Name"))
    status = await _provider(watch, target_up=True, config=config).get_status()
    assert status.notices == ()


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
    seen_notices: set[StatusNotice] = set()
    for step in range(len(SCENARIO) + 1):
        now[0] = step * STEP_SECONDS
        status = await provider.get_status()
        assert status.checked_at == NOW
        seen_notices.update(status.notices)
        for info in (status.source, status.target, status.vm):
            seen_states.add(info.state)
            if info.reason is not None:
                seen_reasons.add(info.reason)
    # Konzept 6.4: jeder Zustand, jede Ursache und jeder Hinweis mindestens einmal.
    assert seen_states == set(ResourceState)
    assert seen_reasons == set(StatusReason)
    # Die Hinweise zu Klein kommen aus dem Rundlauf des Wächters (GUARD_SCENARIO).
    guard_notices = {notice for step in GUARD_SCENARIO for notice in step.notices}
    assert seen_notices | guard_notices == set(StatusNotice)
    assert not seen_notices & guard_notices
    # Nach einem Durchlauf beginnt der Ablauf von vorn.
    assert provider.step() == 0


async def test_mock_scenario_shows_the_next_attempt() -> None:
    """Bei Fehlern der Statusverbindung zeigt der Rundlauf den nächsten Versuch (ab v0.4.4)."""
    now = [0.0]
    provider = MockStatusProvider(monotonic=lambda: now[0], clock=lambda: NOW)
    seen_paused: set[bool] = set()
    for step, (_source, _target, vm, _notices) in enumerate(SCENARIO):
        started = NOW + timedelta(seconds=step * STEP_SECONDS)
        now[0] = step * STEP_SECONDS + 7.5
        retry = (await provider.get_status()).watch_retry
        if vm.reason in PERMANENT_REASONS:
            assert retry == WatchRetry(at=started + timedelta(minutes=30), paused=True)
        elif vm.reason in TRANSIENT_REASONS:
            assert retry == WatchRetry(at=started + timedelta(seconds=STEP_SECONDS), paused=False)
        else:
            assert retry is None
        # Innerhalb eines Schritts bleibt der Zeitpunkt gleich.
        now[0] += 5
        assert (await provider.get_status()).watch_retry == retry
        if retry is not None:
            seen_paused.add(retry.paused)
    assert seen_paused == {True, False}


def test_factory_by_mode(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    mock = build_integrations(RunMode.MOCK, CONFIG, paths, tmp_path, None)
    assert isinstance(mock.status, MockLab)
    assert mock.control is mock.status
    assert mock.background == ()
    assert mock.status_changed is None
    assert isinstance(mock.source_guard, MockSourceGuard)
    # Der simulierte Wächter nimmt auch die Aufträge des Probelaufs an.
    assert mock.guard_channel is mock.source_guard
    real = build_integrations(RunMode.REAL, CONFIG, paths, tmp_path, None)
    assert isinstance(real.status, RealStatusProvider)
    assert isinstance(real.control, RealControl)
    # Im Modus real laufen die Sitzung vm-watch und das Lesen des Wächters im Hintergrund.
    assert isinstance(real.source_guard, RealSourceGuard)
    assert isinstance(real.guard_channel, RealGuardChannel)
    assert len(real.background) == 2
    assert real.status_changed is not None
    # Den Rundlauf gibt es nur in der Simulation.
    (tmp_path / "mock-tour").touch()
    assert mock.tour_active() is True
    assert real.tour_active() is False
