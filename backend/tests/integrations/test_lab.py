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
from twinraid.shared.status import ResourceState, StatusReason

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _lab(tmp_path: Path, clock: Clock) -> MockLab:
    return MockLab("Gaming", tmp_path, LabTimings(), monotonic=clock, clock=lambda: NOW)


async def test_starts_switched_off(tmp_path: Path) -> None:
    status = await _lab(tmp_path, Clock()).get_status()
    assert status.source.reason is StatusReason.RUNS_HERE
    assert status.target.state is ResourceState.OFFLINE
    assert status.vm.reason is StatusReason.TARGET_OFFLINE
    assert status.checked_at == NOW


async def test_boot_api_vm_and_shutdown_timings(tmp_path: Path) -> None:
    clock = Clock()
    lab = _lab(tmp_path, clock)
    await lab.wake()
    clock.now = 19.9
    assert (await lab.get_status()).target.state is ResourceState.OFFLINE
    clock.now = 20
    status = await lab.get_status()
    assert status.target.state is ResourceState.ONLINE
    assert status.vm.reason is StatusReason.API_UNREACHABLE
    clock.now = 30
    assert (await lab.get_status()).vm.state is ResourceState.OFFLINE
    vm = await lab.find_vm()
    await lab.start_vm(vm)
    clock.now = 33
    assert (await lab.get_status()).vm.state is ResourceState.ONLINE
    await lab.shutdown_vm(vm)
    clock.now = 41
    assert (await lab.get_status()).vm.state is ResourceState.OFFLINE
    await lab.power_off()
    clock.now = 55
    assert (await lab.get_status()).target.state is ResourceState.ONLINE
    clock.now = 56
    assert (await lab.get_status()).target.state is ResourceState.OFFLINE


async def test_faults(tmp_path: Path) -> None:
    clock = Clock()
    lab = _lab(tmp_path, clock)
    set_flag(fault_flag(tmp_path, "no-boot"), True)
    await lab.wake()
    clock.now = 100
    assert (await lab.get_status()).target.state is ResourceState.OFFLINE
    set_flag(fault_flag(tmp_path, "no-boot"), False)
    await lab.wake()
    clock.now = 200
    vm = await lab.find_vm()
    set_flag(fault_flag(tmp_path, "api-forbidden"), True)
    with pytest.raises(ActionError) as excinfo:
        await lab.start_vm(vm)
    assert excinfo.value.outcome is ActionOutcome.API_FORBIDDEN
    set_flag(fault_flag(tmp_path, "ssh-denied"), True)
    with pytest.raises(ActionError) as excinfo:
        await lab.power_off()
    assert excinfo.value.outcome is ActionOutcome.SSH_DENIED


def test_fault_names_are_fixed(tmp_path: Path) -> None:
    assert FAULTS == ("vm-hang", "api-forbidden", "ssh-denied", "no-boot", "other-vm")
    with pytest.raises(ValueError, match="unknown fault"):
        fault_flag(tmp_path, "../etc")


async def test_tour_replaces_the_simulation(tmp_path: Path) -> None:
    lab = _lab(tmp_path, Clock())
    set_flag(tour_flag(tmp_path), True)
    assert lab.tour_active() is True
    source, target, vm = SCENARIO[0]
    status = await lab.get_status()
    assert (status.source, status.target, status.vm) == (source, target, vm)
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
    set_flag(fault_flag(tmp_path, "other-vm"), True)
    assert (await lab.get_status()).other_vms_running == ("Test-VM",)
    assert await lab.other_running_vms() == ("Test-VM",)
