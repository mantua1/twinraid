"""Interaktive Simulation (Konzept 6.4, Entscheidung E11)."""

from datetime import UTC, datetime
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
from twinraid.integrations.mocks.status import SCENARIO
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import ResourceState, StatusNotice, StatusReason

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
