"""Aktionen im Worker: Sperrlogik, Verfügbarkeit, Zwischenzustände und Abläufe (Konzept 4.2)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab, fault_flag, set_flag, tour_flag
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome, ActionStep
from twinraid.shared.mode import RunMode
from twinraid.shared.status import ResourceState, StatusReason
from twinraid.worker.action_log import ActionLog
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.monitor import StatusMonitor

pytestmark = pytest.mark.anyio
BASE = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
CONFIG = load_config(EXAMPLE_CONFIG)


class FakeTime:
    """Simulierte Zeit. auto: jedes sleep() springt sofort vorwärts; sonst bewegt nur advance()."""

    def __init__(self, auto: bool = True) -> None:
        self.now = 0.0
        self.auto = auto

    def monotonic(self) -> float:
        return self.now

    def clock(self) -> datetime:
        return BASE + timedelta(seconds=self.now)

    async def sleep(self, seconds: float) -> None:
        if self.auto:
            self.now += seconds
            await asyncio.sleep(0)
            return
        target = self.now + seconds
        # Simulierte Zeit: Nur advance() im Test bewegt sie weiter.
        while self.now < target:  # noqa: ASYNC110
            await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Setup:
    def __init__(
        self,
        tmp_path: Path,
        time: FakeTime,
        mode: RunMode = RunMode.MOCK,
        config: TwinRaidConfig = CONFIG,
    ) -> None:
        self.dir = tmp_path
        self.time = time
        self.lab = MockLab(
            config.gaming_vm.unraid_vm_name, tmp_path, monotonic=time.monotonic, clock=time.clock
        )
        self.monitor = StatusMonitor(self.lab, clock=time.clock)
        self.jobs = MockJobSwitch(tmp_path / "mock-job-running")
        self.log_path = tmp_path / "actions.jsonl"
        self.actions = ActionCoordinator(
            self.lab,
            self.monitor,
            self.jobs,
            config,
            mode,
            ActionLog(self.log_path, clock=time.clock),
            tour_active=self.lab.tour_active,
            clock=time.clock,
            monotonic=time.monotonic,
            sleep=time.sleep,
        )

    async def refresh(self) -> None:
        await self.monitor.check_once()

    async def request(self, name: ActionName) -> ActionBlocker | None:
        await self.refresh()
        return await self.actions.request(name)

    async def settle(self) -> None:
        for _ in range(10_000):
            if not self.actions.busy:
                return
            await asyncio.sleep(0)
        pytest.fail("action did not finish")

    async def run(self, name: ActionName) -> None:
        assert await self.request(name) is None
        await self.settle()

    def availability(self) -> dict[ActionName, ActionBlocker | None]:
        return self.actions.snapshot(self.monitor.current())[1].availability

    def last(self, resource: str) -> ActionOutcome | None:
        _, actions = self.actions.snapshot(self.monitor.current())
        result = actions.target.last if resource == "target" else actions.vm.last
        return result.outcome if result is not None else None

    def log(self) -> list[tuple[str, str, str, str | None]]:
        entries = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [(e["action"], e["trigger"], e["result"], e.get("part_of")) for e in entries]

    async def boot(self) -> None:
        """Groß einschalten und warten, bis die API bereit ist."""
        await self.run(ActionName.WAKE_TARGET)
        await self.refresh()

    async def start_vm(self) -> None:
        await self.run(ActionName.VM_START)
        await self.refresh()


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path, FakeTime())


async def spin(times: int = 200) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


# --- Verfügbarkeit ---


async def test_nothing_is_possible_before_the_first_check(setup: Setup) -> None:
    assert set(setup.availability().values()) == {ActionBlocker.TARGET_UNKNOWN}


async def test_availability_with_target_off(setup: Setup) -> None:
    await setup.refresh()
    assert setup.availability() == {
        ActionName.WAKE_TARGET: None,
        ActionName.SHUTDOWN_TARGET: ActionBlocker.TARGET_OFFLINE,
        ActionName.VM_START: None,
        ActionName.VM_STOP: ActionBlocker.TARGET_OFFLINE,
        ActionName.VM_FORCE_STOP: ActionBlocker.TARGET_OFFLINE,
    }


async def test_availability_with_vm_running(setup: Setup) -> None:
    await setup.boot()
    await setup.start_vm()
    assert setup.availability() == {
        ActionName.WAKE_TARGET: ActionBlocker.TARGET_ONLINE,
        ActionName.SHUTDOWN_TARGET: None,
        ActionName.VM_START: ActionBlocker.VM_RUNNING,
        ActionName.VM_STOP: None,
        ActionName.VM_FORCE_STOP: ActionBlocker.FORCE_NOT_YET,
    }


async def test_real_mode_needs_the_switches(tmp_path: Path) -> None:
    setup = Setup(tmp_path, FakeTime(), mode=RunMode.REAL)
    await setup.refresh()
    assert set(setup.availability().values()) == {ActionBlocker.NOT_ENABLED}
    assert setup.actions.enabled_groups() == []
    assert await setup.actions.request(ActionName.WAKE_TARGET) is ActionBlocker.NOT_ENABLED


async def test_vm_start_with_target_off_also_needs_wake(tmp_path: Path) -> None:
    config = CONFIG.model_copy(update={"actions": CONFIG.actions.model_copy(update={"vm": True})})
    setup = Setup(tmp_path, FakeTime(), mode=RunMode.REAL, config=config)
    await setup.refresh()
    availability = setup.availability()
    assert availability[ActionName.WAKE_TARGET] is ActionBlocker.NOT_ENABLED
    assert availability[ActionName.VM_START] is ActionBlocker.NOT_ENABLED
    assert setup.actions.enabled_groups() == ["vm"]


async def test_shutdown_is_locked_while_a_sync_runs(setup: Setup) -> None:
    await setup.boot()
    setup.jobs.set_running(True)
    assert setup.availability()[ActionName.SHUTDOWN_TARGET] is ActionBlocker.SYNC_RUNNING


async def test_shutdown_is_locked_while_the_vm_state_is_unknown(setup: Setup) -> None:
    await setup.run(ActionName.WAKE_TARGET)
    # Groß ist erreichbar, die API antwortet noch nicht.
    setup.time.now = 0
    setup.lab._since = 0  # direkt in den Zustand „API startet“
    await setup.refresh()
    status = setup.monitor.current()
    assert status.vm.reason is StatusReason.API_UNREACHABLE
    availability = setup.availability()
    assert availability[ActionName.SHUTDOWN_TARGET] is ActionBlocker.VM_UNKNOWN
    # Der VM-Start wartet dagegen auf die API.
    assert availability[ActionName.VM_START] is None


async def test_tour_blocks_everything(setup: Setup) -> None:
    set_flag(tour_flag(setup.dir), True)
    await setup.refresh()
    assert set(setup.availability().values()) == {ActionBlocker.TOUR}


# --- Abläufe ---


async def test_wake_target(setup: Setup) -> None:
    await setup.run(ActionName.WAKE_TARGET)
    assert setup.last("target") is ActionOutcome.SUCCESS
    assert setup.monitor.current().target.state is ResourceState.ONLINE
    assert setup.log() == [
        ("wake_target", "dashboard", "started", None),
        ("wake_target", "dashboard", "success", None),
    ]


async def test_wake_shows_intermediate_states(tmp_path: Path) -> None:
    time = FakeTime(auto=False)
    setup = Setup(tmp_path, time)
    assert await setup.request(ActionName.WAKE_TARGET) is None
    await spin()
    status, actions = setup.actions.snapshot(setup.monitor.current())
    assert status.target.state is ResourceState.STARTING
    assert status.vm.reason is StatusReason.TARGET_STARTING
    assert actions.target.running is not None
    assert actions.target.running.step is ActionStep.WAKING
    assert actions.availability[ActionName.WAKE_TARGET] is ActionBlocker.BUSY
    assert await setup.actions.request(ActionName.VM_START) is ActionBlocker.BUSY
    # Groß bootet (20 s), danach braucht die API noch 10 s.
    for _ in range(12):
        time.advance(3)
        await spin()
    await setup.settle()
    assert setup.last("target") is ActionOutcome.SUCCESS


async def test_wake_times_out(setup: Setup) -> None:
    set_flag(fault_flag(setup.dir, "no-boot"), True)
    await setup.run(ActionName.WAKE_TARGET)
    assert setup.last("target") is ActionOutcome.BOOT_TIMEOUT
    # boot_timeout_minutes aus der Konfiguration (5 Minuten).
    assert setup.time.now >= 5 * 60


async def test_vm_start_wakes_the_target_first(setup: Setup) -> None:
    await setup.run(ActionName.VM_START)
    await setup.refresh()
    assert setup.monitor.current().vm.state is ResourceState.ONLINE
    assert setup.last("vm") is ActionOutcome.SUCCESS
    assert setup.last("target") is ActionOutcome.SUCCESS
    assert setup.log() == [
        ("vm_start", "dashboard", "started", None),
        ("wake_target", "automatic", "started", "vm_start"),
        ("wake_target", "automatic", "success", "vm_start"),
        ("vm_start", "dashboard", "success", None),
    ]


async def test_vm_start_without_permission(setup: Setup) -> None:
    """Rechtetest: Ein Key ohne VMS:UPDATE_ANY wird verständlich gemeldet (Konzept 5.6)."""
    await setup.boot()
    set_flag(fault_flag(setup.dir, "api-forbidden"), True)
    await setup.run(ActionName.VM_START)
    assert setup.last("vm") is ActionOutcome.API_FORBIDDEN
    await setup.refresh()
    assert setup.monitor.current().vm.state is ResourceState.OFFLINE
    assert setup.log()[-1] == ("vm_start", "dashboard", "api_forbidden", None)


async def test_vm_stop(setup: Setup) -> None:
    await setup.boot()
    await setup.start_vm()
    await setup.run(ActionName.VM_STOP)
    await setup.refresh()
    assert setup.last("vm") is ActionOutcome.SUCCESS
    assert setup.monitor.current().vm.state is ResourceState.OFFLINE
    assert setup.actions.force_available_at() is None


async def test_force_only_after_the_timeout(tmp_path: Path) -> None:
    time = FakeTime(auto=False)
    setup = Setup(tmp_path, time)
    time.auto = True
    await setup.boot()
    await setup.start_vm()
    set_flag(fault_flag(setup.dir, "vm-hang"), True)
    time.auto = False
    stop_requested = time.now
    assert await setup.request(ActionName.VM_STOP) is None
    await spin()
    status, actions = setup.actions.snapshot(setup.monitor.current())
    assert status.vm.state is ResourceState.STOPPING
    assert actions.force_available_at == time.clock() + timedelta(minutes=3)
    assert actions.availability[ActionName.VM_FORCE_STOP] is ActionBlocker.BUSY
    # shutdown_timeout_minutes: 3 Minuten
    while setup.actions.busy:
        time.advance(3)
        await spin()
    assert time.now - stop_requested >= 180
    assert setup.last("vm") is ActionOutcome.VM_STOP_TIMEOUT
    await setup.refresh()
    assert setup.availability()[ActionName.VM_FORCE_STOP] is None
    time.auto = True
    await setup.run(ActionName.VM_FORCE_STOP)
    await setup.refresh()
    assert setup.last("vm") is ActionOutcome.SUCCESS
    assert setup.monitor.current().vm.state is ResourceState.OFFLINE
    assert setup.actions.force_available_at() is None


async def test_force_is_not_offered_without_a_stop(setup: Setup) -> None:
    await setup.boot()
    await setup.start_vm()
    setup.time.advance(3600)
    assert await setup.request(ActionName.VM_FORCE_STOP) is ActionBlocker.FORCE_NOT_YET


async def test_failed_acpi_signal_does_not_offer_force(setup: Setup) -> None:
    await setup.boot()
    await setup.start_vm()
    set_flag(fault_flag(setup.dir, "ssh-denied"), True)
    await setup.run(ActionName.VM_STOP)
    assert setup.last("vm") is ActionOutcome.SSH_DENIED
    setup.time.advance(3600)
    await setup.refresh()
    assert setup.availability()[ActionName.VM_FORCE_STOP] is ActionBlocker.FORCE_NOT_YET


async def test_shutdown_stops_the_vm_first(setup: Setup) -> None:
    await setup.boot()
    await setup.start_vm()
    await setup.run(ActionName.SHUTDOWN_TARGET)
    await setup.refresh()
    assert setup.last("target") is ActionOutcome.SUCCESS
    assert setup.monitor.current().target.state is ResourceState.OFFLINE
    assert setup.log()[-4:] == [
        ("shutdown_target", "dashboard", "started", None),
        ("vm_stop", "automatic", "started", "shutdown_target"),
        ("vm_stop", "automatic", "success", "shutdown_target"),
        ("shutdown_target", "dashboard", "success", None),
    ]


async def test_shutdown_aborts_when_the_vm_keeps_running(setup: Setup) -> None:
    """Entscheidung E10: kein Herunterfahren an der VM vorbei."""
    await setup.boot()
    await setup.start_vm()
    set_flag(fault_flag(setup.dir, "vm-hang"), True)
    await setup.run(ActionName.SHUTDOWN_TARGET)
    await setup.refresh()
    assert setup.last("target") is ActionOutcome.VM_STOP_TIMEOUT
    assert setup.monitor.current().target.state is ResourceState.ONLINE
    assert setup.monitor.current().vm.state is ResourceState.ONLINE
    assert setup.availability()[ActionName.VM_FORCE_STOP] is None


async def test_shutdown_without_vm(setup: Setup) -> None:
    await setup.boot()
    await setup.run(ActionName.SHUTDOWN_TARGET)
    assert setup.last("target") is ActionOutcome.SUCCESS
    assert ("vm_stop", "automatic", "started", "shutdown_target") not in setup.log()


async def test_shutdown_shows_stopping(tmp_path: Path) -> None:
    time = FakeTime(auto=False)
    setup = Setup(tmp_path, time)
    time.auto = True
    await setup.boot()
    time.auto = False
    assert await setup.request(ActionName.SHUTDOWN_TARGET) is None
    await spin()
    status, _ = setup.actions.snapshot(setup.monitor.current())
    assert status.target.state is ResourceState.STOPPING
    while setup.actions.busy:
        time.advance(3)
        await spin()
    assert setup.last("target") is ActionOutcome.SUCCESS


async def test_close_marks_running_actions_as_interrupted(tmp_path: Path) -> None:
    setup = Setup(tmp_path, FakeTime(auto=False))
    assert await setup.request(ActionName.WAKE_TARGET) is None
    await spin()
    await setup.actions.close()
    assert setup.log()[-1] == ("wake_target", "dashboard", "interrupted", None)
