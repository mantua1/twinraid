"""Aktionen im Worker: Sperrlogik, Verfügbarkeit, Zwischenzustände und Abläufe (Konzept 4.2)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.watch import StubWatch, report
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.control import ActionError, RealControl
from twinraid.integrations.jobs import NoJobs
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab, fault_flag, set_flag, tour_flag
from twinraid.integrations.ssh import SshPower, SshTarget
from twinraid.integrations.status import RealStatusProvider
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome, ActionStep
from twinraid.shared.mode import RunMode
from twinraid.shared.status import ResourceInfo, ResourceState, StatusReason
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
        lab: MockLab | None = None,
    ) -> None:
        self.dir = tmp_path
        self.time = time
        self.lab = lab or MockLab(tmp_path, monotonic=time.monotonic, clock=time.clock)
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
        """Groß einschalten und warten, bis der VM-Dienst bereit ist."""
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
    availability = setup.availability()
    assert availability.pop(ActionName.SHUTDOWN_TARGET_FORCE_VM) is ActionBlocker.NOT_OFFERED
    assert set(availability.values()) == {ActionBlocker.TARGET_UNKNOWN}


async def test_availability_with_target_off(setup: Setup) -> None:
    await setup.refresh()
    assert setup.availability() == {
        ActionName.WAKE_TARGET: None,
        ActionName.SHUTDOWN_TARGET: ActionBlocker.TARGET_OFFLINE,
        ActionName.SHUTDOWN_TARGET_FORCE_VM: ActionBlocker.NOT_OFFERED,
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
        ActionName.SHUTDOWN_TARGET_FORCE_VM: ActionBlocker.NOT_OFFERED,
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
    # Groß ist erreichbar, der VM-Dienst antwortet noch nicht.
    setup.time.now = 0
    setup.lab._since = 0  # direkt in den Zustand „VM-Dienst startet“
    await setup.refresh()
    status = setup.monitor.current()
    assert status.vm.reason is StatusReason.VM_SERVICE_UNAVAILABLE
    availability = setup.availability()
    assert availability[ActionName.SHUTDOWN_TARGET] is ActionBlocker.VM_UNKNOWN
    # Der VM-Start wartet dagegen auf den VM-Dienst.
    assert availability[ActionName.VM_START] is None


@pytest.mark.parametrize("fault", ["vm-not-configured", "guard-outdated"])
async def test_vm_actions_are_locked_without_a_vm_state(setup: Setup, fault: str) -> None:
    """Eine Ursache, die kein Warten behebt, sperrt auch den VM-Start."""
    await setup.boot()
    set_flag(fault_flag(setup.dir, fault), True)
    await setup.refresh()
    availability = setup.availability()
    for name in (ActionName.VM_START, ActionName.VM_STOP, ActionName.SHUTDOWN_TARGET):
        assert availability[name] is ActionBlocker.VM_UNKNOWN


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
    # Groß bootet (20 s), danach braucht der VM-Dienst noch 10 s.
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


async def test_vm_start_denied_by_guard(setup: Setup) -> None:
    """guard.sh lehnt ab (z. B. falsche Zeile in authorized_keys): verständlich gemeldet."""
    await setup.boot()
    set_flag(fault_flag(setup.dir, "ssh-denied"), True)
    await setup.run(ActionName.VM_START)
    assert setup.last("vm") is ActionOutcome.SSH_DENIED
    await setup.refresh()
    assert setup.monitor.current().vm.state is ResourceState.OFFLINE
    assert setup.log()[-1] == ("vm_start", "dashboard", "ssh_denied", None)


async def test_vm_start_waits_for_the_vm_service(tmp_path: Path) -> None:
    """Nach dem Wecken meldet der Schritt waiting_for_vm_service, bis vm-watch antwortet."""
    time = FakeTime()
    setup = Setup(tmp_path, time)
    await setup.run(ActionName.WAKE_TARGET)
    set_flag(fault_flag(setup.dir, "libvirt-down"), True)
    await setup.refresh()
    assert setup.monitor.current().vm.reason is StatusReason.VM_SERVICE_UNAVAILABLE
    time.auto = False
    assert await setup.request(ActionName.VM_START) is None
    await spin()
    _, actions = setup.actions.snapshot(setup.monitor.current())
    assert actions.vm.running is not None
    assert actions.vm.running.step is ActionStep.WAITING_FOR_VM_SERVICE
    time.auto = True
    time.advance(3)
    await setup.settle()
    # Der VM-Dienst kam nicht: Zeitlimit boot_timeout_minutes.
    assert setup.last("vm") is ActionOutcome.BOOT_TIMEOUT


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


# --- Erzwingen und danach Server herunterfahren (Konzept 4.2, Ergänzung zu E10) ---


async def _aborted_shutdown(setup: Setup) -> None:
    """Groß herunterfahren scheitert an einer VM, die nicht reagiert."""
    await setup.boot()
    await setup.start_vm()
    set_flag(fault_flag(setup.dir, "vm-hang"), True)
    await setup.run(ActionName.SHUTDOWN_TARGET)
    await setup.refresh()
    assert setup.last("target") is ActionOutcome.VM_STOP_TIMEOUT


def _offer(setup: Setup) -> datetime | None:
    return setup.actions.snapshot(setup.monitor.current())[1].forced_shutdown_until


async def test_aborted_shutdown_offers_force_and_shutdown(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    until = _offer(setup)
    assert until is not None
    # Gültig ab dem Abbruch für 10 Minuten.
    assert until == setup.time.clock() + timedelta(minutes=10)
    availability = setup.availability()
    assert availability[ActionName.SHUTDOWN_TARGET_FORCE_VM] is None
    # Das einfache Erzwingen bleibt ebenfalls möglich.
    assert availability[ActionName.VM_FORCE_STOP] is None


async def test_force_and_shutdown(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    await setup.run(ActionName.SHUTDOWN_TARGET_FORCE_VM)
    await setup.refresh()
    status = setup.monitor.current()
    assert status.target.state is ResourceState.OFFLINE
    assert setup.last("target") is ActionOutcome.SUCCESS
    assert setup.last("vm") is ActionOutcome.SUCCESS
    assert setup.log()[-6:] == [
        ("shutdown_target_force_vm", "dashboard", "started", None),
        ("vm_force_stop", "automatic", "started", "shutdown_target_force_vm"),
        ("vm_force_stop", "automatic", "success", "shutdown_target_force_vm"),
        ("shutdown_target", "automatic", "started", "shutdown_target_force_vm"),
        ("shutdown_target", "automatic", "success", "shutdown_target_force_vm"),
        ("shutdown_target_force_vm", "dashboard", "success", None),
    ]
    assert _offer(setup) is None


async def test_force_and_shutdown_shows_intermediate_states(tmp_path: Path) -> None:
    time = FakeTime()
    setup = Setup(tmp_path, time)
    await _aborted_shutdown(setup)
    time.auto = False
    assert await setup.request(ActionName.SHUTDOWN_TARGET_FORCE_VM) is None
    status, actions = setup.actions.snapshot(setup.monitor.current())
    assert actions.vm.running is not None
    assert actions.vm.running.step is ActionStep.FORCING_VM
    assert status.vm.state is ResourceState.STOPPING
    assert actions.availability[ActionName.SHUTDOWN_TARGET_FORCE_VM] is ActionBlocker.BUSY
    while setup.actions.busy:
        time.advance(3)
        await spin()
    assert setup.last("target") is ActionOutcome.SUCCESS


async def test_plain_vm_stop_timeout_offers_nothing(setup: Setup) -> None:
    await setup.boot()
    await setup.start_vm()
    set_flag(fault_flag(setup.dir, "vm-hang"), True)
    await setup.run(ActionName.VM_STOP)
    await setup.refresh()
    assert setup.last("vm") is ActionOutcome.VM_STOP_TIMEOUT
    assert _offer(setup) is None
    assert await setup.request(ActionName.SHUTDOWN_TARGET_FORCE_VM) is ActionBlocker.NOT_OFFERED


async def test_offer_expires_after_ten_minutes(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    setup.time.advance(9 * 60 + 59)
    await setup.refresh()
    assert _offer(setup) is not None
    setup.time.advance(1)
    await setup.refresh()
    assert _offer(setup) is None
    assert await setup.request(ActionName.SHUTDOWN_TARGET_FORCE_VM) is ActionBlocker.NOT_OFFERED


async def test_offer_expires_when_the_vm_is_off(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    setup.lab._vm_state = "SHUTOFF"  # die VM fährt doch noch von selbst herunter
    await setup.refresh()
    assert _offer(setup) is None
    # Auch wenn die VM wieder läuft, bleibt das Angebot weg.
    setup.lab._vm_state = "RUNNING"
    await setup.refresh()
    assert _offer(setup) is None


async def test_offer_expires_when_the_target_is_off(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    setup.lab._power = "off"  # Groß wurde anderweitig ausgeschaltet
    await setup.refresh()
    assert _offer(setup) is None


@pytest.mark.parametrize("other", [ActionName.VM_FORCE_STOP, ActionName.VM_STOP])
async def test_offer_expires_with_another_action(setup: Setup, other: ActionName) -> None:
    await _aborted_shutdown(setup)
    assert await setup.request(other) is None
    assert _offer(setup) is None
    await setup.settle()
    await setup.refresh()
    assert setup.availability()[ActionName.SHUTDOWN_TARGET_FORCE_VM] is ActionBlocker.NOT_OFFERED


async def test_offer_is_lost_on_restart(setup: Setup) -> None:
    """Das Angebot liegt nur im Speicher des Workers (Konzept 4.2)."""
    await _aborted_shutdown(setup)
    restarted = Setup(setup.dir, setup.time, lab=setup.lab)
    await restarted.refresh()
    status = restarted.monitor.current()
    assert status.target.state is ResourceState.ONLINE
    assert status.vm.state is ResourceState.ONLINE
    availability = restarted.availability()
    assert availability[ActionName.SHUTDOWN_TARGET_FORCE_VM] is ActionBlocker.NOT_OFFERED


async def test_offer_needs_vm_and_shutdown_in_real_mode(tmp_path: Path) -> None:
    only_shutdown = CONFIG.actions.model_copy(update={"shutdown": True})
    setup = Setup(
        tmp_path,
        FakeTime(),
        mode=RunMode.REAL,
        config=CONFIG.model_copy(update={"actions": only_shutdown}),
    )
    await setup.refresh()
    assert setup.availability()[ActionName.SHUTDOWN_TARGET_FORCE_VM] is ActionBlocker.NOT_ENABLED


async def test_offer_is_locked_while_a_sync_runs(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    setup.jobs.set_running(True)
    assert setup.availability()[ActionName.SHUTDOWN_TARGET_FORCE_VM] is ActionBlocker.SYNC_RUNNING


async def test_failed_force_keeps_the_target_on(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    set_flag(fault_flag(setup.dir, "ssh-denied"), True)
    await setup.run(ActionName.SHUTDOWN_TARGET_FORCE_VM)
    await setup.refresh()
    assert setup.last("target") is ActionOutcome.SSH_DENIED
    assert setup.monitor.current().target.state is ResourceState.ONLINE
    assert (
        "shutdown_target",
        "automatic",
        "started",
        "shutdown_target_force_vm",
    ) not in setup.log()
    # Das Angebot ist verbraucht.
    assert _offer(setup) is None


# --- Schutz vor weiteren VMs (Konzept 4.2, ab v0.4.2) ---


async def test_other_vm_blocks_shutdown(setup: Setup) -> None:
    await setup.boot()
    set_flag(fault_flag(setup.dir, "other-vm"), True)
    await setup.refresh()
    assert setup.monitor.current().other_vms_running == ("Test-VM",)
    availability = setup.availability()
    assert availability[ActionName.SHUTDOWN_TARGET] is ActionBlocker.OTHER_VMS_RUNNING
    set_flag(fault_flag(setup.dir, "other-vm"), False)
    await setup.refresh()
    assert setup.availability()[ActionName.SHUTDOWN_TARGET] is None


async def test_unknown_vm_list_blocks_shutdown(setup: Setup) -> None:
    await setup.refresh()
    status = setup.monitor.current().model_copy(
        update={
            "target": ResourceInfo(state=ResourceState.ONLINE),
            "vm": ResourceInfo(state=ResourceState.OFFLINE),
            "other_vms_running": None,
        }
    )
    assert setup.actions.availability(status)[ActionName.SHUTDOWN_TARGET] is (
        ActionBlocker.VM_UNKNOWN
    )


async def test_shutdown_checks_again_before_stopping_the_vm(setup: Setup) -> None:
    """Der Status war noch ohne weitere VM; der Worker prüft selbst noch einmal."""
    await setup.boot()
    await setup.start_vm()
    set_flag(fault_flag(setup.dir, "other-vm"), True)
    assert await setup.actions.request(ActionName.SHUTDOWN_TARGET) is None
    await setup.settle()
    await setup.refresh()
    assert setup.last("target") is ActionOutcome.OTHER_VMS_RUNNING
    status = setup.monitor.current()
    assert status.target.state is ResourceState.ONLINE
    # Die Gaming-VM wurde gar nicht erst heruntergefahren.
    assert status.vm.state is ResourceState.ONLINE
    assert setup.log()[-1] == ("shutdown_target", "dashboard", "other_vms_running", None)
    assert _offer(setup) is None


async def test_shutdown_checks_again_right_before_poweroff(tmp_path: Path) -> None:
    time = FakeTime()
    setup = Setup(tmp_path, time)
    await setup.boot()
    await setup.start_vm()
    time.auto = False
    assert await setup.request(ActionName.SHUTDOWN_TARGET) is None
    await spin()
    # Während die Gaming-VM herunterfährt, startet jemand eine weitere VM.
    set_flag(fault_flag(setup.dir, "other-vm"), True)
    while setup.actions.busy:
        time.advance(3)
        await spin()
    await setup.refresh()
    status = setup.monitor.current()
    assert setup.last("target") is ActionOutcome.OTHER_VMS_RUNNING
    assert status.target.state is ResourceState.ONLINE
    assert status.vm.state is ResourceState.OFFLINE
    assert setup.log()[-3:] == [
        ("vm_stop", "automatic", "started", "shutdown_target"),
        ("vm_stop", "automatic", "success", "shutdown_target"),
        ("shutdown_target", "dashboard", "other_vms_running", None),
    ]


async def test_force_and_shutdown_never_forces_while_another_vm_runs(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    set_flag(fault_flag(setup.dir, "other-vm"), True)
    # Noch der alte Status: angenommen, aber der Worker prüft vor dem Erzwingen.
    assert await setup.actions.request(ActionName.SHUTDOWN_TARGET_FORCE_VM) is None
    await setup.settle()
    await setup.refresh()
    status = setup.monitor.current()
    assert setup.last("target") is ActionOutcome.OTHER_VMS_RUNNING
    assert status.vm.state is ResourceState.ONLINE
    assert status.target.state is ResourceState.ONLINE
    assert ("vm_force_stop", "automatic", "started", "shutdown_target_force_vm") not in setup.log()


async def test_force_and_shutdown_is_blocked_by_another_vm(setup: Setup) -> None:
    await _aborted_shutdown(setup)
    set_flag(fault_flag(setup.dir, "other-vm"), True)
    await setup.refresh()
    assert setup.availability()[ActionName.SHUTDOWN_TARGET_FORCE_VM] is (
        ActionBlocker.OTHER_VMS_RUNNING
    )


# --- Prüfung in guard.sh vor poweroff (Konzept 3.5, ab v0.4.3) ---


class RefusingLab(MockLab):
    """guard.sh lehnt poweroff ab, obwohl der Worker keine laufende VM sieht."""

    def __init__(self, directory: Path, time: FakeTime, refusal: ActionOutcome) -> None:
        super().__init__(directory, monotonic=time.monotonic, clock=time.clock)
        self.refusal = refusal

    async def power_off(self) -> None:
        raise ActionError(self.refusal)


@pytest.mark.parametrize(
    "refusal",
    [
        ActionOutcome.VM_RUNNING,
        ActionOutcome.OTHER_VMS_RUNNING,
        ActionOutcome.VM_SERVICE_UNAVAILABLE,
    ],
)
async def test_guard_refusal_keeps_the_target_on(tmp_path: Path, refusal: ActionOutcome) -> None:
    time = FakeTime()
    setup = Setup(tmp_path, time, lab=RefusingLab(tmp_path, time, refusal))
    await setup.boot()
    await setup.run(ActionName.SHUTDOWN_TARGET)
    await setup.refresh()
    assert setup.last("target") is refusal
    assert setup.monitor.current().target.state is ResourceState.ONLINE
    assert setup.log()[-1] == ("shutdown_target", "dashboard", refusal.value, None)


# --- Keine Anmeldung während der Abläufe außer dem Befehl selbst (Konzept 3.5) ---


class RecordingPower(SshPower):
    """Ersetzt die Anmeldung mit dem Key „power“ und wirkt auf die nachgebaute Sitzung."""

    def __init__(self, tmp_path: Path, watch: StubWatch, host: dict[str, bool]) -> None:
        super().__init__(SshTarget("192.0.2.11", 22, tmp_path / "key", tmp_path / "known"))
        self.watch = watch
        self.host = host
        self.requests: list[str] = []

    async def _request(self, request: str, time_limit: float) -> str:
        self.requests.append(request)
        effects = {"vm-start": "RUNNING", "vm-shutdown": "SHUTOFF", "vm-force-stop": "SHUTOFF"}
        if request in effects:
            self.watch.show(report(effects[request]))
        if request == "poweroff":
            self.host["up"] = False
        return ""


class RealSetup:
    """Echte Steuerung mit nachgebauter Sitzung vm-watch und ohne ssh-Prozess."""

    def __init__(self, tmp_path: Path, vm: str) -> None:
        self.time = FakeTime()
        self.host = {"up": True}
        self.watch = StubWatch()
        self.watch.show(report(vm))
        self.power = RecordingPower(tmp_path, self.watch, self.host)

        async def tcp(_host: str, _port: int) -> bool:
            return self.host["up"]

        config = CONFIG.model_copy(
            update={"actions": CONFIG.actions.model_copy(update={"vm": True, "shutdown": True})}
        )
        control = RealControl(config, self.watch, self.power, None, tcp_check=tcp)
        self.monitor = StatusMonitor(
            RealStatusProvider(config, self.watch, tcp_check=tcp, clock=self.time.clock),
            clock=self.time.clock,
        )
        self.actions = ActionCoordinator(
            control,
            self.monitor,
            NoJobs(),
            config,
            RunMode.REAL,
            ActionLog(tmp_path / "actions.jsonl", clock=self.time.clock),
            clock=self.time.clock,
            monotonic=self.time.monotonic,
            sleep=self.time.sleep,
        )

    async def run(self, name: ActionName) -> ActionOutcome | None:
        await self.monitor.check_once()
        assert await self.actions.request(name) is None
        for _ in range(10_000):
            if not self.actions.busy:
                break
            await asyncio.sleep(0)
        _, snapshot = self.actions.snapshot(self.monitor.current())
        last = snapshot.target.last or snapshot.vm.last
        return last.outcome if last is not None else None


@pytest.mark.parametrize(
    ("vm", "action", "requests"),
    [
        ("SHUTOFF", ActionName.VM_START, ["vm-start"]),
        ("RUNNING", ActionName.VM_STOP, ["vm-shutdown"]),
        ("RUNNING", ActionName.SHUTDOWN_TARGET, ["vm-shutdown", "poweroff"]),
        ("SHUTOFF", ActionName.SHUTDOWN_TARGET, ["poweroff"]),
    ],
)
async def test_sequences_log_in_only_for_the_command(
    tmp_path: Path, vm: str, action: ActionName, requests: list[str]
) -> None:
    setup = RealSetup(tmp_path, vm)
    assert await setup.run(action) is ActionOutcome.SUCCESS
    assert setup.power.requests == requests
