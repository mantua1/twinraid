"""Automatisches Herunterfahren von Groß (Konzept 4.2, Entscheidung E30, ab v0.6.0)."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import EXAMPLE_CONFIG
from tests.worker.test_actions import FakeTime, Setup, spin
from twinraid.config import TwinRaidConfig
from twinraid.integrations.mocks.lab import fault_flag, set_flag
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome, Trigger
from twinraid.shared.mode import RunMode
from twinraid.shared.schedule import AutoShutdownKind, AutoShutdownReason, AutoShutdownStatus
from twinraid.shared.status import ResourceInfo, ResourceState, ResourceStatus, StatusReason
from twinraid.worker.auto_shutdown import AutoShutdown

pytestmark = pytest.mark.anyio

ON = ResourceInfo(state=ResourceState.ONLINE)
OFF = ResourceInfo(state=ResourceState.OFFLINE)


def _config(
    play: bool = True,
    play_delay: int = 15,
    sync_delay: int = 5,
    shutdown_after: bool = True,
    actions: dict[str, bool] | None = None,
) -> TwinRaidConfig:
    # Any: verschachtelte YAML-Daten werden im Test gezielt verändert.
    data: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["gaming_vm"]["shutdown_target_after"] = {"enabled": play, "delay_minutes": play_delay}
    data["sync"]["schedule"].update(
        shutdown_after=shutdown_after, shutdown_delay_minutes=sync_delay
    )
    if actions is not None:
        data["actions"].update(actions)
    return TwinRaidConfig.model_validate(data)


class Auto:
    """Aktionen mit Simulation und das automatische Herunterfahren mit derselben Uhr."""

    def __init__(
        self, tmp_path: Path, config: TwinRaidConfig | None = None, mode: RunMode = RunMode.MOCK
    ) -> None:
        config = config or _config()
        self.time = FakeTime()
        self.setup = Setup(tmp_path, self.time, mode=mode, config=config)
        self.auto = AutoShutdown(config, mode, self.setup.monitor, clock=self.time.clock)
        self.auto.attach(self.setup.actions, self.setup.jobs)
        self.setup.actions.attach_auto_shutdown(self.auto)

    async def tick(self) -> None:
        await self.setup.refresh()
        self.auto.tick()
        await spin()
        await self.setup.settle()
        await self.setup.refresh()

    async def play_until_vm_off(self) -> None:
        """VM starten (weckt Groß) und wieder herunterfahren."""
        await self.setup.run(ActionName.VM_START)
        await self.setup.refresh()
        await self.setup.run(ActionName.VM_STOP)
        await self.setup.refresh()

    def status(self) -> Any:  # noqa: ANN401 - AutoShutdownStatus | None, kurz für Tests
        return self.auto.snapshot(self.setup.monitor.current())

    def target(self) -> ResourceState:
        return self.setup.monitor.current().target.state


@pytest.fixture
def auto(tmp_path: Path) -> Auto:
    return Auto(tmp_path)


# --- Nach dem Spielen ---


async def test_after_playing_target_is_shut_down_after_the_delay(auto: Auto) -> None:
    await auto.play_until_vm_off()
    assert auto.auto.offered()
    await auto.tick()  # Die Wartezeit beginnt mit SHUTOFF.
    status = auto.status()
    assert status.kinds == (AutoShutdownKind.AFTER_PLAY,)
    assert status.reason is None
    assert status.at == auto.time.clock() + timedelta(minutes=15)
    auto.time.advance(15 * 60 - 1)
    await auto.tick()
    assert auto.target() is ResourceState.ONLINE
    auto.time.advance(1)
    await auto.tick()
    assert auto.target() is ResourceState.OFFLINE
    assert not auto.auto.offered()
    assert auto.setup.log()[-2:] == [
        ("shutdown_target", "automatic", "started", "vm_start"),
        ("shutdown_target", "automatic", "success", "vm_start"),
    ]
    _, actions = auto.setup.actions.snapshot(auto.setup.monitor.current())
    assert actions.target.last is not None
    assert actions.target.last.trigger is Trigger.AUTOMATIC


async def test_no_mark_when_the_target_was_already_on(auto: Auto) -> None:
    await auto.setup.boot()
    await auto.play_until_vm_off()
    assert not auto.auto.offered()


async def test_no_mark_when_play_shutdown_is_off(tmp_path: Path) -> None:
    auto = Auto(tmp_path, _config(play=False))
    await auto.play_until_vm_off()
    assert not auto.auto.offered()
    assert auto.status() is None


async def test_wake_button_sets_no_mark(auto: Auto) -> None:
    await auto.setup.boot()
    assert not auto.auto.offered()


async def test_vm_running_holds_the_delay(auto: Auto) -> None:
    await auto.setup.run(ActionName.VM_START)
    await auto.tick()
    assert auto.status().reason is AutoShutdownReason.VM_RUNNING
    auto.time.advance(3600)
    await auto.tick()
    assert auto.target() is ResourceState.ONLINE


async def test_delay_restarts_when_the_vm_starts_again(auto: Auto) -> None:
    await auto.play_until_vm_off()
    await auto.tick()
    auto.time.advance(10 * 60)
    await auto.tick()
    # Die VM läuft wieder (ein neues Spiel), dann wieder aus: die 15 min beginnen neu.
    await auto.setup.run(ActionName.VM_START)
    await auto.tick()
    await auto.setup.run(ActionName.VM_STOP)
    await auto.tick()
    restarted = auto.time.clock()
    assert auto.status().at == restarted + timedelta(minutes=15)
    auto.time.advance(10 * 60)
    await auto.tick()
    assert auto.target() is ResourceState.ONLINE
    auto.time.advance(5 * 60)
    await auto.tick()
    assert auto.target() is ResourceState.OFFLINE


async def test_other_vm_blocks_and_restarts_the_delay(auto: Auto) -> None:
    await auto.play_until_vm_off()
    await auto.tick()
    set_flag(fault_flag(auto.setup.dir, "other-vm"), True)
    auto.time.advance(20 * 60)
    await auto.tick()
    assert auto.status().reason is AutoShutdownReason.OTHER_VMS_RUNNING
    assert auto.target() is ResourceState.ONLINE
    set_flag(fault_flag(auto.setup.dir, "other-vm"), False)
    await auto.tick()
    auto.time.advance(15 * 60)
    await auto.tick()
    assert auto.target() is ResourceState.OFFLINE


async def test_sync_running_holds_the_delay(auto: Auto) -> None:
    await auto.play_until_vm_off()
    auto.setup.jobs.set_running(True)
    await auto.tick()
    assert auto.status().reason is AutoShutdownReason.SYNC_RUNNING
    auto.time.advance(3600)
    await auto.tick()
    assert auto.target() is ResourceState.ONLINE
    auto.setup.jobs.set_running(False)
    await auto.tick()
    auto.time.advance(15 * 60)
    await auto.tick()
    assert auto.target() is ResourceState.OFFLINE


async def test_guard_refusal_drops_the_mark_without_a_second_try(auto: Auto) -> None:
    await auto.play_until_vm_off()
    await auto.tick()
    set_flag(fault_flag(auto.setup.dir, "ssh-denied"), True)
    auto.time.advance(15 * 60)
    await auto.tick()
    assert auto.target() is ResourceState.ONLINE
    assert auto.setup.last("target") is ActionOutcome.SSH_DENIED
    assert not auto.auto.offered()
    auto.time.advance(3600)
    await auto.tick()
    assert [entry for entry in auto.setup.log() if entry[1] == "automatic"][-2:] == [
        ("shutdown_target", "automatic", "started", "vm_start"),
        ("shutdown_target", "automatic", "ssh_denied", "vm_start"),
    ]


async def test_target_offline_drops_the_mark(auto: Auto) -> None:
    await auto.play_until_vm_off()
    assert auto.auto.offered()
    # Groß wird von Hand ausgeschaltet: Es gibt nichts mehr herunterzufahren.
    await auto.setup.lab.power_off()
    auto.time.advance(60)
    await auto.tick()
    assert auto.target() is ResourceState.OFFLINE
    assert not auto.auto.offered()
    assert auto.status() is None


# --- Bedingungen einzeln ---


def _status(
    target: ResourceInfo = ON,
    vm: ResourceInfo = OFF,
    others: tuple[str, ...] | None = (),
) -> ResourceStatus:
    return ResourceStatus(
        source=ON, target=target, vm=vm, checked_at=None, other_vms_running=others
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (_status(), None),
        (_status(vm=ON), AutoShutdownReason.VM_RUNNING),
        (
            _status(vm=ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_PAUSED)),
            AutoShutdownReason.VM_RUNNING,
        ),
        (
            _status(vm=ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_SUSPENDED)),
            AutoShutdownReason.VM_RUNNING,
        ),
        # CRASHED zählt nicht als aus (Konzept 4.2).
        (
            _status(vm=ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.VM_CRASHED)),
            AutoShutdownReason.VM_RUNNING,
        ),
        (_status(vm=ResourceInfo(state=ResourceState.STOPPING)), AutoShutdownReason.VM_RUNNING),
        (
            _status(
                vm=ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.WATCH_INTERRUPTED)
            ),
            AutoShutdownReason.VM_UNKNOWN,
        ),
        (_status(target=ResourceInfo(state=ResourceState.UNKNOWN)), AutoShutdownReason.VM_UNKNOWN),
        (_status(others=None), AutoShutdownReason.VM_UNKNOWN),
        (_status(others=("Linux",)), AutoShutdownReason.OTHER_VMS_RUNNING),
    ],
)
def test_conditions(tmp_path: Path, status: ResourceStatus, reason: AutoShutdownReason) -> None:
    auto = Auto(tmp_path)
    assert auto.auto.reason(status) is reason


async def test_busy_and_not_enabled(tmp_path: Path) -> None:
    auto = Auto(tmp_path)
    await auto.setup.boot()
    assert await auto.setup.actions.request(ActionName.VM_START) is None
    assert auto.auto.reason(_status()) is AutoShutdownReason.BUSY
    await auto.setup.settle()
    real = Auto(tmp_path / "real", _config(actions={"shutdown": False}), mode=RunMode.REAL)
    assert real.auto.reason(_status()) is AutoShutdownReason.NOT_ENABLED


# --- Manuelle Aktionen, Groß anlassen, beide Vormerkungen ---


def _snapshot(auto: AutoShutdown) -> AutoShutdownStatus:
    status = auto.snapshot(_status())
    assert status is not None
    return status


def test_manual_actions_clear_the_right_marks(tmp_path: Path) -> None:
    auto = Auto(tmp_path).auto
    for command in ("vm_start", "vm_stop", "vm_force_stop"):
        auto.mark_after_sync()
        auto.mark_after_play()
        auto.on_manual(command)
        assert _snapshot(auto).kinds == (AutoShutdownKind.AFTER_PLAY,)
        auto.cancel()
    for command in ("shutdown_target", "shutdown_target_force_vm"):
        auto.mark_after_sync()
        auto.on_manual(command)
        assert not auto.offered()
    for command in ("sync_dry_run", "sync_start", "sync_cancel", "sync_confirm_deletions"):
        auto.mark_after_sync()
        auto.mark_after_play()
        auto.on_manual(command)
        assert not auto.offered()
    # Wecken und „Groß anlassen“ selbst löschen über on_manual nichts.
    auto.mark_after_play()
    auto.on_manual("wake_target")
    assert auto.offered()


async def test_keep_target_on(auto: Auto) -> None:
    availability = auto.setup.actions.availability(auto.setup.monitor.current())
    assert availability[ActionName.AUTO_SHUTDOWN_CANCEL] is ActionBlocker.NOT_OFFERED
    assert (
        await auto.setup.actions.request(ActionName.AUTO_SHUTDOWN_CANCEL)
        is ActionBlocker.NOT_OFFERED
    )
    await auto.play_until_vm_off()
    availability = auto.setup.actions.availability(auto.setup.monitor.current())
    assert availability[ActionName.AUTO_SHUTDOWN_CANCEL] is None
    assert await auto.setup.actions.request(ActionName.AUTO_SHUTDOWN_CANCEL) is None
    assert not auto.auto.offered()
    assert auto.setup.log()[-1] == ("auto_shutdown_cancel", "dashboard", "success", None)
    auto.time.advance(3600)
    await auto.tick()
    assert auto.target() is ResourceState.ONLINE


async def test_keep_target_on_needs_no_enablement(tmp_path: Path) -> None:
    auto = Auto(tmp_path, _config(actions={"shutdown": False}), mode=RunMode.REAL)
    auto.auto.mark_after_play()
    availability = auto.setup.actions.availability(auto.setup.monitor.current())
    assert availability[ActionName.AUTO_SHUTDOWN_CANCEL] is None


def test_both_marks_use_the_longer_delay(tmp_path: Path) -> None:
    auto = Auto(tmp_path, _config(play_delay=15, sync_delay=40)).auto
    auto.mark_after_play()
    assert _snapshot(auto).at == auto._clock() + timedelta(minutes=15)
    auto.mark_after_sync()
    status = _snapshot(auto)
    assert status.kinds == (AutoShutdownKind.AFTER_PLAY, AutoShutdownKind.AFTER_SYNC)
    assert status.at == auto._clock() + timedelta(minutes=40)


def test_after_sync_needs_shutdown_after(tmp_path: Path) -> None:
    auto = Auto(tmp_path, _config(shutdown_after=False)).auto
    auto.mark_after_sync()
    assert not auto.offered()


def test_restart_forgets_the_marks(tmp_path: Path) -> None:
    first = Auto(tmp_path)
    first.auto.mark_after_play()
    assert not Auto(tmp_path / "again").auto.offered()


async def test_vm_running_at_execution_keeps_the_target_on(auto: Auto) -> None:
    """Startet die VM zwischen Prüfung und Ausführung, fährt TwinRaid sie nie selbst herunter."""
    await auto.setup.boot()
    await auto.setup.start_vm()
    assert auto.setup.actions.start_automatic_shutdown(ActionName.VM_START)
    await auto.setup.settle()
    await auto.setup.refresh()
    assert auto.setup.last("target") is ActionOutcome.VM_RUNNING
    assert auto.target() is ResourceState.ONLINE
    assert auto.setup.monitor.current().vm.state is ResourceState.ONLINE
