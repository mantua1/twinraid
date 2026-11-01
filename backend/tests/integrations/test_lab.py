"""Interaktive Simulation (Konzept 6.4, Entscheidung E11)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from twinraid.integrations.control import ActionError
from twinraid.integrations.mocks.lab import (
    FAULTS,
    LabTimings,
    MockLab,
    fault_flag,
    set_flag,
    tour_flag,
)
from twinraid.integrations.mocks.status import MOCK_ARC_MAX, MOCK_C_MAX_UNLIMITED, SCENARIO
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import (
    ResourceState,
    StatusNotice,
    StatusReason,
    TargetZfs,
    WatchRetry,
)

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _lab(tmp_path: Path, clock: Clock) -> MockLab:
    return MockLab(tmp_path, LabTimings(), monotonic=clock, clock=lambda: NOW)


async def _booted(tmp_path: Path) -> tuple[MockLab, Clock]:
    clock = Clock()
    lab = _lab(tmp_path, clock)
    await lab.wake()
    clock.now = 30
    return lab, clock


def _fault(tmp_path: Path, name: str, on: bool = True) -> None:
    set_flag(fault_flag(tmp_path, name), on)


async def test_starts_switched_off(tmp_path: Path) -> None:
    status = await _lab(tmp_path, Clock()).get_status()
    assert status.source.reason is StatusReason.RUNS_HERE
    assert status.target.state is ResourceState.OFFLINE
    assert status.vm.reason is StatusReason.TARGET_OFFLINE
    assert status.checked_at == NOW
    assert status.notices == ()


async def test_boot_vm_service_vm_and_shutdown_timings(tmp_path: Path) -> None:
    clock = Clock()
    lab = _lab(tmp_path, clock)
    await lab.wake()
    clock.now = 19.9
    assert (await lab.get_status()).target.state is ResourceState.OFFLINE
    clock.now = 20
    status = await lab.get_status()
    assert status.target.state is ResourceState.ONLINE
    assert status.vm.reason is StatusReason.VM_SERVICE_UNAVAILABLE
    with pytest.raises(ActionError) as excinfo:
        await lab.vm_state()
    assert excinfo.value.outcome is ActionOutcome.VM_SERVICE_UNAVAILABLE
    clock.now = 30
    assert (await lab.get_status()).vm.state is ResourceState.OFFLINE
    assert await lab.vm_state() == "SHUTOFF"
    await lab.start_vm()
    clock.now = 33
    assert (await lab.get_status()).vm.state is ResourceState.ONLINE
    await lab.shutdown_vm()
    clock.now = 41
    assert (await lab.get_status()).vm.state is ResourceState.OFFLINE
    await lab.power_off()
    clock.now = 55
    assert (await lab.get_status()).target.state is ResourceState.ONLINE
    clock.now = 56
    assert (await lab.get_status()).target.state is ResourceState.OFFLINE


async def test_vm_state_while_the_target_is_off(tmp_path: Path) -> None:
    lab = _lab(tmp_path, Clock())
    with pytest.raises(ActionError) as excinfo:
        await lab.vm_state()
    assert excinfo.value.outcome is ActionOutcome.WATCH_INTERRUPTED


async def test_no_boot_and_ssh_denied(tmp_path: Path) -> None:
    clock = Clock()
    lab = _lab(tmp_path, clock)
    _fault(tmp_path, "no-boot")
    await lab.wake()
    clock.now = 100
    assert (await lab.get_status()).target.state is ResourceState.OFFLINE
    _fault(tmp_path, "no-boot", on=False)
    await lab.wake()
    clock.now = 200
    _fault(tmp_path, "ssh-denied")
    for action in (lab.start_vm, lab.shutdown_vm, lab.force_stop_vm, lab.power_off):
        with pytest.raises(ActionError) as excinfo:
            await action()
        assert excinfo.value.outcome is ActionOutcome.SSH_DENIED


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("vm-not-configured", StatusReason.VM_NOT_CONFIGURED),
        ("libvirt-down", StatusReason.VM_SERVICE_UNAVAILABLE),
        ("guard-outdated", StatusReason.GUARD_OUTDATED),
        ("status-key-rejected", StatusReason.SSH_AUTH_FAILED),
    ],
)
async def test_guard_faults_hide_the_vm(tmp_path: Path, fault: str, reason: StatusReason) -> None:
    lab, _clock = await _booted(tmp_path)
    _fault(tmp_path, fault)
    status = await lab.get_status()
    assert status.target.state is ResourceState.ONLINE
    assert status.vm.state is ResourceState.UNKNOWN
    assert status.vm.reason is reason
    assert status.other_vms_running is None
    for request in (lab.vm_state, lab.other_running_vms):
        with pytest.raises(ActionError) as excinfo:
            await request()
        assert excinfo.value.outcome.value == reason.value


@pytest.mark.parametrize(
    ("fault", "outcome"),
    [
        ("vm-not-configured", ActionOutcome.VM_NOT_CONFIGURED),
        ("guard-outdated", ActionOutcome.SSH_DENIED),
    ],
)
async def test_guard_faults_stop_vm_commands(
    tmp_path: Path, fault: str, outcome: ActionOutcome
) -> None:
    lab, _clock = await _booted(tmp_path)
    _fault(tmp_path, fault)
    for action in (lab.start_vm, lab.shutdown_vm, lab.force_stop_vm):
        with pytest.raises(ActionError) as excinfo:
            await action()
        assert excinfo.value.outcome is outcome


async def test_poweroff_is_refused_like_guard_sh(tmp_path: Path) -> None:
    lab, clock = await _booted(tmp_path)
    await lab.start_vm()
    clock.now = 40
    with pytest.raises(ActionError) as excinfo:
        await lab.power_off()
    assert excinfo.value.outcome is ActionOutcome.VM_RUNNING
    await lab.force_stop_vm()
    _fault(tmp_path, "other-vm")
    with pytest.raises(ActionError) as excinfo:
        await lab.power_off()
    assert excinfo.value.outcome is ActionOutcome.OTHER_VMS_RUNNING
    _fault(tmp_path, "other-vm", on=False)
    _fault(tmp_path, "libvirt-down")
    with pytest.raises(ActionError) as excinfo:
        await lab.power_off()
    assert excinfo.value.outcome is ActionOutcome.VM_SERVICE_UNAVAILABLE
    _fault(tmp_path, "libvirt-down", on=False)
    await lab.power_off()
    clock.now = 60
    assert (await lab.get_status()).target.state is ResourceState.OFFLINE


async def test_connect_plugin_notices(tmp_path: Path) -> None:
    lab, _clock = await _booted(tmp_path)
    _fault(tmp_path, "connect-plugin")
    assert (await lab.get_status()).notices == (
        StatusNotice.TARGET_CONNECT_PLUGIN,
        StatusNotice.TARGET_FLASH_BACKUP,
    )
    # Ist Groß aus, gibt es auch keinen Hinweis.
    await lab.power_off()
    _clock.now = 100
    assert (await lab.get_status()).notices == ()


def test_fault_names_are_fixed(tmp_path: Path) -> None:
    assert FAULTS == (
        "vm-hang",
        "ssh-denied",
        "no-boot",
        "other-vm",
        "vm-not-configured",
        "libvirt-down",
        "guard-outdated",
        "connect-plugin",
        "status-key-rejected",
        "source-guard-inactive",
        "source-guard-lost",
        "source-guard-start-failed",
        "sync-deletions",
        "sync-empty-source",
        "sync-target-refused",
        "target-exists",
        "target-clone",
        "sync-send-error",
        "sync-slow",
        "vm-device-busy",
        "vm-memory-low",
        "arc-unlimited",
    )
    for name in ("../etc", "api-forbidden"):
        with pytest.raises(ValueError, match="unknown fault"):
            fault_flag(tmp_path, name)


async def test_tour_replaces_the_simulation(tmp_path: Path) -> None:
    lab = _lab(tmp_path, Clock())
    set_flag(tour_flag(tmp_path), True)
    assert lab.tour_active() is True
    source, target, vm, notices = SCENARIO[0]
    status = await lab.get_status()
    assert (status.source, status.target, status.vm, status.notices) == (
        source,
        target,
        vm,
        notices,
    )
    set_flag(tour_flag(tmp_path), False)
    assert lab.tour_active() is False


async def test_other_vm_fault(tmp_path: Path) -> None:
    clock = Clock()
    lab = _lab(tmp_path, clock)
    assert (await lab.get_status()).other_vms_running is None
    await lab.wake()
    clock.now = 30
    assert (await lab.get_status()).other_vms_running == ()
    assert await lab.other_running_vms() == ()
    _fault(tmp_path, "other-vm")
    assert (await lab.get_status()).other_vms_running == ("Test-VM",)
    assert await lab.other_running_vms() == ("Test-VM",)


@pytest.mark.parametrize("fault", ["status-key-rejected", "guard-outdated"])
async def test_permanent_faults_pause_like_real_mode(tmp_path: Path, fault: str) -> None:
    """Wie die Sitzung vm-watch: 30 Minuten Pause mit dem nächsten Versuch (ab v0.4.4)."""
    clock = Clock()
    wall = [NOW]
    lab = MockLab(tmp_path, LabTimings(), monotonic=clock, clock=lambda: wall[0])
    await lab.wake()
    clock.now = 30
    assert (await lab.get_status()).watch_retry is None
    _fault(tmp_path, fault)
    pause = WatchRetry(at=NOW + timedelta(minutes=30), paused=True)
    assert (await lab.get_status()).watch_retry == pause
    wall[0] = NOW + timedelta(minutes=29, seconds=59)
    assert (await lab.get_status()).watch_retry == pause
    # Nach der Pause scheitert der nächste Versuch wieder: neue Pause.
    wall[0] = NOW + timedelta(minutes=30)
    assert (await lab.get_status()).watch_retry == WatchRetry(
        at=NOW + timedelta(minutes=60), paused=True
    )
    # Fehler aus wirkt wie ein erfolgreiches trust-host-key: sofort wieder ein Stand.
    _fault(tmp_path, fault, on=False)
    status = await lab.get_status()
    assert status.vm.state is ResourceState.OFFLINE
    assert status.watch_retry is None
    # Eine neue Pause beginnt beim nächsten Fehler von vorn.
    _fault(tmp_path, fault)
    assert (await lab.get_status()).watch_retry == WatchRetry(
        at=wall[0] + timedelta(minutes=30), paused=True
    )


@pytest.mark.parametrize("fault", ["vm-not-configured", "libvirt-down", "other-vm"])
async def test_other_faults_need_no_pause(tmp_path: Path, fault: str) -> None:
    """Die Sitzung läuft dabei; nur Fehler der Anmeldung lösen eine Pause aus."""
    lab, _clock = await _booted(tmp_path)
    _fault(tmp_path, fault)
    assert (await lab.get_status()).watch_retry is None


async def test_status_key_rejected_leaves_the_key_power_alone(tmp_path: Path) -> None:
    """Groß lehnt nur den Key „status“ ab; Befehle mit dem Key „power“ kommen an."""
    lab, clock = await _booted(tmp_path)
    _fault(tmp_path, "status-key-rejected")
    await lab.start_vm()
    clock.now = 40
    _fault(tmp_path, "status-key-rejected", on=False)
    assert (await lab.get_status()).vm.state is ResourceState.ONLINE


async def test_no_pause_while_the_target_is_off(tmp_path: Path) -> None:
    lab = _lab(tmp_path, Clock())
    _fault(tmp_path, "status-key-rejected")
    status = await lab.get_status()
    assert status.vm.reason is StatusReason.TARGET_OFFLINE
    assert status.watch_retry is None


async def test_arc_values_only_while_the_target_answers(tmp_path: Path) -> None:
    """Werte des ARC aus der Statuszeile von vm-watch (guard.sh 3, Konzept 3.5)."""
    clock = Clock()
    lab = _lab(tmp_path, clock)
    assert (await lab.get_status()).target_zfs is None
    await lab.wake()
    clock.now = 30
    status = await lab.get_status()
    assert status.target_zfs == TargetZfs(arc_max=MOCK_ARC_MAX, c_max=MOCK_ARC_MAX)
    assert StatusNotice.TARGET_ARC_UNLIMITED not in status.notices
    _fault(tmp_path, "arc-unlimited")
    status = await lab.get_status()
    assert status.target_zfs == TargetZfs(arc_max=0, c_max=MOCK_C_MAX_UNLIMITED)
    assert StatusNotice.TARGET_ARC_UNLIMITED in status.notices
    # Ohne Statuszeile (VM-Dienst nicht bereit) gibt es auch keine Werte des ARC.
    _fault(tmp_path, "libvirt-down")
    status = await lab.get_status()
    assert status.target_zfs is None
    assert StatusNotice.TARGET_ARC_UNLIMITED not in status.notices


@pytest.mark.parametrize(
    ("fault", "outcome"),
    [
        ("vm-memory-low", ActionOutcome.VM_MEMORY_LOW),
        ("vm-device-busy", ActionOutcome.VM_DEVICE_BUSY),
    ],
)
async def test_vm_start_refused_like_guard_sh_3(
    tmp_path: Path, fault: str, outcome: ActionOutcome
) -> None:
    lab, _clock = await _booted(tmp_path)
    _fault(tmp_path, fault)
    with pytest.raises(ActionError) as caught:
        await lab.start_vm()
    assert caught.value.outcome is outcome
    status = await lab.get_status()
    assert status.vm.state is ResourceState.OFFLINE
    # Ein belegtes Gerät heißt: Eine weitere VM läuft und nutzt es.
    others = ("Test-VM",) if fault == "vm-device-busy" else ()
    assert status.other_vms_running == others


async def test_memory_check_comes_first(tmp_path: Path) -> None:
    lab, _clock = await _booted(tmp_path)
    _fault(tmp_path, "vm-memory-low")
    _fault(tmp_path, "vm-device-busy")
    with pytest.raises(ActionError) as caught:
        await lab.start_vm()
    assert caught.value.outcome is ActionOutcome.VM_MEMORY_LOW
