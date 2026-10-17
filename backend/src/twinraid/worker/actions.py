"""Aktionen im Worker: Sperrlogik, Verfügbarkeit und Abläufe (Konzept 4.2).

Ressourcen sind Groß und die VM; jede Aktion belegt eine oder beide. Ein Befehl
für eine belegte Ressource wird abgelehnt, es gibt keine Warteschlange. Die
Abläufe laufen im Hintergrund; der Status zeigt Zwischenzustände, den aktuellen
Schritt und das letzte Ergebnis. Jede Aktion steht im Aktionslog (Konzept 5.3).
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.control import ActionControl, ActionError
from twinraid.integrations.jobs import JobState
from twinraid.shared.actions import (
    ActionBlocker,
    ActionGroup,
    ActionName,
    ActionOutcome,
    ActionResult,
    ActionsSnapshot,
    ActionStep,
    ResourceActivity,
    RunningAction,
    Trigger,
)
from twinraid.shared.mode import RunMode
from twinraid.shared.status import ResourceInfo, ResourceState, ResourceStatus, StatusReason
from twinraid.worker.action_log import STARTED, ActionLog, new_action_id

logger = logging.getLogger(__name__)


class Resource(StrEnum):
    TARGET = "target"
    VM = "vm"


_RESOURCES: dict[ActionName, frozenset[Resource]] = {
    ActionName.WAKE_TARGET: frozenset({Resource.TARGET}),
    ActionName.SHUTDOWN_TARGET: frozenset({Resource.TARGET, Resource.VM}),
    ActionName.SHUTDOWN_TARGET_FORCE_VM: frozenset({Resource.TARGET, Resource.VM}),
    ActionName.VM_START: frozenset({Resource.TARGET, Resource.VM}),
    ActionName.VM_STOP: frozenset({Resource.VM}),
    ActionName.VM_FORCE_STOP: frozenset({Resource.VM}),
}

# libvirt-Zustände, wie guard.sh sie meldet (Konzept 3.5).
_VM_RUNNING = frozenset({"RUNNING", "IDLE"})
_VM_OFF = frozenset({"SHUTOFF", "CRASHED"})
# Groß ist erreichbar, aber sshd, die Sitzung vm-watch oder der VM-Dienst sind (noch)
# nicht bereit: weiter warten.
_TRANSIENT = frozenset(
    {
        ActionOutcome.WATCH_INTERRUPTED,
        ActionOutcome.VM_SERVICE_UNAVAILABLE,
        ActionOutcome.SSH_UNREACHABLE,
        ActionOutcome.SSH_FAILED,
    }
)
# VM starten bleibt mit diesen Ursachen möglich; der Ablauf wartet auf den VM-Dienst.
_TRANSIENT_VM_REASONS = frozenset(
    {
        StatusReason.TARGET_STARTING,
        StatusReason.WATCH_CONNECTING,
        StatusReason.WATCH_INTERRUPTED,
        StatusReason.VM_SERVICE_UNAVAILABLE,
    }
)
# VM-Überlagerung, solange Groß nach dem Wecken bootet.
_VM_WAITING = ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.TARGET_STARTING)
# Die VM gilt als laufend (für das Angebot „Erzwingen und danach Server herunterfahren“).
_VM_STILL_RUNNING = frozenset({ResourceState.ONLINE, ResourceState.STOPPING})
# So lange gilt das Angebot nach einem abgebrochenen Herunterfahren (Konzept 4.2, E10).
FORCED_SHUTDOWN_OFFER = timedelta(minutes=10)


@dataclass(frozen=True)
class ActionTimings:
    """Feste Zeiten der Abläufe (Konzept 4.2)."""

    poll_seconds: float = 3.0
    vm_start_confirm_seconds: float = 60.0
    force_confirm_seconds: float = 30.0
    shutdown_wait_seconds: float = 15 * 60.0


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...

    async def check_once(self) -> ResourceStatus: ...


@dataclass
class _Run:
    id: str
    action: ActionName
    started_at: datetime
    step: ActionStep
    # Diese Aktion hat Groß geweckt: Groß gilt bis zur Bereitschaft als „Startet …“.
    woke: bool = False
    held: frozenset[Resource] = field(default_factory=frozenset)

    def public(self) -> RunningAction:
        return RunningAction(action=self.action, step=self.step, started_at=self.started_at)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ActionCoordinator:
    def __init__(
        self,
        control: ActionControl,
        status: StatusSource,
        jobs: JobState,
        config: TwinRaidConfig,
        mode: RunMode,
        action_log: ActionLog,
        tour_active: Callable[[], bool] = lambda: False,
        timings: ActionTimings | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._control = control
        self._status = status
        self._jobs = jobs
        self._config = config
        self._mode = mode
        self._log = action_log
        self._tour_active = tour_active
        self._timings = timings or ActionTimings()
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._running: dict[Resource, _Run] = {}
        self._last: dict[Resource, ActionResult] = {}
        # Zeitpunkt des letzten erfolgreich gesendeten ACPI-Signals (für Erzwingen).
        self._stop_sent_at: datetime | None = None
        # Ende des Angebots „Erzwingen und danach Server herunterfahren“; nur im Speicher.
        self._forced_shutdown_until: datetime | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Freischaltung und Verfügbarkeit ---

    def enabled(self, group: ActionGroup) -> bool:
        """Im Simulationsmodus ist alles verfügbar; im Modus real nur actions.* (Konzept 4.2)."""
        if self._mode is RunMode.MOCK:
            return True
        return bool(getattr(self._config.actions, group.value))

    def enabled_groups(self) -> list[ActionGroup]:
        return [group for group in ActionGroup if self.enabled(group)]

    @property
    def busy(self) -> bool:
        """Läuft gerade eine Aktion?"""
        return bool(self._running)

    @property
    def _shutdown_timeout(self) -> timedelta:
        return timedelta(minutes=self._config.gaming_vm.shutdown_timeout_minutes)

    def force_available_at(self) -> datetime | None:
        if self._stop_sent_at is None:
            return None
        return self._stop_sent_at + self._shutdown_timeout

    def forced_shutdown_until(self, status: ResourceStatus) -> datetime | None:
        """Gilt das Angebot noch? Es verfällt nach Ablauf und bei jeder Zustandsänderung."""
        until = self._forced_shutdown_until
        if until is None:
            return None
        still_valid = (
            self._clock() < until
            and status.target.state is ResourceState.ONLINE
            and status.vm.state in _VM_STILL_RUNNING
        )
        if not still_valid:
            self._forced_shutdown_until = None
            return None
        return until

    def availability(self, status: ResourceStatus) -> dict[ActionName, ActionBlocker | None]:
        if self._tour_active():
            return dict.fromkeys(ActionName, ActionBlocker.TOUR)
        return {name: self._blocker(name, status) for name in ActionName}

    def _blocker(self, name: ActionName, status: ResourceStatus) -> ActionBlocker | None:
        target, vm = status.target.state, status.vm
        on, off = ResourceState.ONLINE, ResourceState.OFFLINE
        vm_unknown = vm.state is ResourceState.UNKNOWN
        busy = any(resource in self._running for resource in _RESOURCES[name])
        # Schutz vor weiteren VMs beim Ausschalten von Groß (Konzept 4.2, ab v0.4.2).
        other_vms = [
            (status.other_vms_running is None, ActionBlocker.VM_UNKNOWN),
            (bool(status.other_vms_running), ActionBlocker.OTHER_VMS_RUNNING),
        ]
        match name:
            case ActionName.WAKE_TARGET:
                checks = [
                    (not self.enabled(ActionGroup.WAKE), ActionBlocker.NOT_ENABLED),
                    (busy, ActionBlocker.BUSY),
                    (target is on, ActionBlocker.TARGET_ONLINE),
                    (target is not off, ActionBlocker.TARGET_UNKNOWN),
                ]
            case ActionName.SHUTDOWN_TARGET:
                checks = [
                    (not self.enabled(ActionGroup.SHUTDOWN), ActionBlocker.NOT_ENABLED),
                    (busy, ActionBlocker.BUSY),
                    (self._jobs.is_running(), ActionBlocker.SYNC_RUNNING),
                    (target is off, ActionBlocker.TARGET_OFFLINE),
                    (target is not on, ActionBlocker.TARGET_UNKNOWN),
                    (vm_unknown, ActionBlocker.VM_UNKNOWN),
                    *other_vms,
                    (
                        vm.state is not off and not self.enabled(ActionGroup.VM),
                        ActionBlocker.NOT_ENABLED,
                    ),
                ]
            case ActionName.VM_START:
                checks = [
                    (not self.enabled(ActionGroup.VM), ActionBlocker.NOT_ENABLED),
                    (busy, ActionBlocker.BUSY),
                    (
                        target is off and not self.enabled(ActionGroup.WAKE),
                        ActionBlocker.NOT_ENABLED,
                    ),
                    (target is not off and target is not on, ActionBlocker.TARGET_UNKNOWN),
                    (
                        target is on and vm.state not in (off, ResourceState.UNKNOWN),
                        ActionBlocker.VM_RUNNING,
                    ),
                    # Gerade gestartetes Groß: Der Ablauf wartet auf den VM-Dienst.
                    (
                        target is on and vm_unknown and vm.reason not in _TRANSIENT_VM_REASONS,
                        ActionBlocker.VM_UNKNOWN,
                    ),
                ]
            case ActionName.SHUTDOWN_TARGET_FORCE_VM:
                enabled = self.enabled(ActionGroup.VM) and self.enabled(ActionGroup.SHUTDOWN)
                checks = [
                    (not enabled, ActionBlocker.NOT_ENABLED),
                    (busy, ActionBlocker.BUSY),
                    (self._jobs.is_running(), ActionBlocker.SYNC_RUNNING),
                    (self.forced_shutdown_until(status) is None, ActionBlocker.NOT_OFFERED),
                    *other_vms,
                ]
            case ActionName.VM_STOP | ActionName.VM_FORCE_STOP:
                force = name is ActionName.VM_FORCE_STOP
                available_at = self.force_available_at()
                # Läuft das Herunterfahren noch, nennt die Oberfläche so die Uhrzeit, ab der
                # Erzwingen möglich ist, statt nur „belegt“ (Konzept 4.2, ab v0.4.4).
                waiting = (
                    force
                    and available_at is not None
                    and self._clock() < available_at
                    and target is on
                    and vm.state in _VM_STILL_RUNNING
                )
                checks = [
                    (not self.enabled(ActionGroup.VM), ActionBlocker.NOT_ENABLED),
                    (waiting, ActionBlocker.FORCE_NOT_YET),
                    (busy, ActionBlocker.BUSY),
                    (target is off, ActionBlocker.TARGET_OFFLINE),
                    (target is not on, ActionBlocker.TARGET_UNKNOWN),
                    (vm.state is off, ActionBlocker.VM_OFF),
                    (vm_unknown, ActionBlocker.VM_UNKNOWN),
                    (
                        force and (available_at is None or self._clock() < available_at),
                        ActionBlocker.FORCE_NOT_YET,
                    ),
                ]
        return next((blocker for failed, blocker in checks if failed), None)

    # --- Status ---

    def snapshot(self, status: ResourceStatus) -> tuple[ResourceStatus, ActionsSnapshot]:
        """Status mit Zwischenzuständen und der Aktionsteil von get_status."""
        if status.vm.state is ResourceState.OFFLINE and Resource.VM not in self._running:
            self._stop_sent_at = None
        actions = ActionsSnapshot(
            availability=self.availability(status),
            target=self._activity(Resource.TARGET),
            vm=self._activity(Resource.VM),
            force_available_at=self.force_available_at(),
            forced_shutdown_until=self.forced_shutdown_until(status),
        )
        if self._tour_active():
            return status, actions
        return self._overlay(status), actions

    def _activity(self, resource: Resource) -> ResourceActivity:
        run = self._running.get(resource)
        return ResourceActivity(
            running=run.public() if run is not None else None, last=self._last.get(resource)
        )

    def _overlay(self, status: ResourceStatus) -> ResourceStatus:
        target, vm = status.target, status.vm
        target_run = self._running.get(Resource.TARGET)
        vm_run = self._running.get(Resource.VM)
        booting = target_run is not None and (
            target_run.step is ActionStep.WAKING
            or (target_run.step is ActionStep.WAITING_FOR_VM_SERVICE and target_run.woke)
        )
        if booting:
            target = ResourceInfo(state=ResourceState.STARTING)
            if vm.state is not ResourceState.ONLINE:
                vm = _VM_WAITING
        elif target_run is not None and target_run.step is ActionStep.SHUTTING_DOWN:
            target = ResourceInfo(state=ResourceState.STOPPING)
        if vm_run is not None:
            match vm_run.step:
                case ActionStep.WAKING | ActionStep.WAITING_FOR_VM_SERVICE:
                    vm = ResourceInfo(
                        state=ResourceState.STARTING,
                        reason=StatusReason.TARGET_STARTING if booting else None,
                    )
                case ActionStep.STARTING_VM:
                    vm = ResourceInfo(state=ResourceState.STARTING)
                case ActionStep.STOPPING_VM | ActionStep.FORCING_VM:
                    vm = ResourceInfo(state=ResourceState.STOPPING)
                case ActionStep.SHUTTING_DOWN:
                    pass
        return status.model_copy(update={"target": target, "vm": vm})

    # --- Befehle ---

    async def request(self, name: ActionName) -> ActionBlocker | None:
        """Startet eine Aktion im Hintergrund. None: angenommen; sonst der Grund der Ablehnung."""
        blocker = self.availability(self._status.current())[name]
        if blocker is not None:
            return blocker
        # Jede neue Aktion beendet das Angebot; die erzwungene Variante verbraucht es.
        self._forced_shutdown_until = None
        resources = _RESOURCES[name]
        run = _Run(new_action_id(), name, self._clock(), _first_step(name), held=resources)
        for resource in resources:
            self._running[resource] = run
        task = asyncio.create_task(self._execute(run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def close(self) -> None:
        """Beendet laufende Aktionen (Worker stoppt); sie stehen als interrupted im Log."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _execute(self, run: _Run) -> None:
        self._log.record(run.id, run.action, Trigger.DASHBOARD, STARTED)
        outcome = ActionOutcome.SUCCESS
        try:
            await self._sequence(run)
        except ActionError as failure:
            outcome = failure.outcome
        except asyncio.CancelledError:
            self._log.record(run.id, run.action, Trigger.DASHBOARD, ActionOutcome.INTERRUPTED)
            raise
        except Exception:  # Eine Aktion darf den Worker nie beenden.
            logger.exception("action %s failed unexpectedly", run.action.value)
            outcome = ActionOutcome.INTERNAL_ERROR
        self._log.record(run.id, run.action, Trigger.DASHBOARD, outcome)
        result = ActionResult(action=run.action, outcome=outcome, finished_at=self._clock())
        for resource in run.held:
            self._last[resource] = result
        if run.action is ActionName.SHUTDOWN_TARGET and outcome is ActionOutcome.VM_STOP_TIMEOUT:
            # Nur dieser Abbruch erzeugt das Angebot (Ergänzung zu E10).
            self._forced_shutdown_until = result.finished_at + FORCED_SHUTDOWN_OFFER
        # Erst den Status auffrischen, dann freigeben: Die Buttons passen sofort zum neuen Stand.
        with contextlib.suppress(Exception):
            await self._status.check_once()
        for resource in run.held:
            if self._running.get(resource) is run:
                del self._running[resource]

    async def _sequence(self, run: _Run) -> None:
        match run.action:
            case ActionName.WAKE_TARGET:
                await self._wake(run)
            case ActionName.VM_START:
                await self._vm_start(run)
            case ActionName.VM_STOP:
                await self._stop_vm(run)
            case ActionName.VM_FORCE_STOP:
                await self._force_stop_vm(run)
            case ActionName.SHUTDOWN_TARGET:
                await self._shutdown_target(run)
            case ActionName.SHUTDOWN_TARGET_FORCE_VM:
                await self._shutdown_target_force_vm(run)

    @contextlib.asynccontextmanager
    async def _part(self, run: _Run, action: ActionName) -> AsyncIterator[None]:
        """Automatischer Teilschritt mit eigenem Eintrag im Aktionslog."""
        self._log.record(run.id, action, Trigger.AUTOMATIC, STARTED, run.action)
        try:
            yield
        except ActionError as failure:
            self._log.record(run.id, action, Trigger.AUTOMATIC, failure.outcome, run.action)
            raise
        self._log.record(run.id, action, Trigger.AUTOMATIC, ActionOutcome.SUCCESS, run.action)

    # --- Abläufe ---

    async def _wake(self, run: _Run) -> None:
        deadline = self._monotonic() + self._config.servers.target.boot_timeout_minutes * 60
        run.step, run.woke = ActionStep.WAKING, True
        await self._control.wake()
        await self._wait_until_ready(run, deadline, need_vm=False)

    async def _wait_until_ready(self, run: _Run, deadline: float, need_vm: bool) -> str | None:
        """Wartet, bis Groß erreichbar ist und der VM-Dienst antwortet (höchstens bis deadline).

        Gibt den Zustand der VM zurück; None, wenn need_vm aus ist und guard.sh einen
        Fehler meldet, der kein Warten behebt (z. B. power.conf fehlt).
        """
        while True:
            if await self._control.target_reachable():
                run.step = ActionStep.WAITING_FOR_VM_SERVICE
                try:
                    return await self._control.vm_state()
                except ActionError as failure:
                    if failure.outcome not in _TRANSIENT:
                        if need_vm:
                            raise
                        return None
            elif run.woke:
                run.step = ActionStep.WAKING
            if self._monotonic() >= deadline:
                raise ActionError(ActionOutcome.BOOT_TIMEOUT)
            await self._sleep(self._timings.poll_seconds)

    async def _vm_start(self, run: _Run) -> None:
        deadline = self._monotonic() + self._config.servers.target.boot_timeout_minutes * 60
        if not await self._control.target_reachable() and self.enabled(ActionGroup.WAKE):
            async with self._part(run, ActionName.WAKE_TARGET):
                run.step, run.woke = ActionStep.WAKING, True
                await self._control.wake()
                await self._wait_until_ready(run, deadline, need_vm=False)
        state = await self._wait_until_ready(run, deadline, need_vm=True)
        if state is None or state in _VM_RUNNING:
            return
        run.step, run.woke = ActionStep.STARTING_VM, False
        self._stop_sent_at = None
        await self._control.start_vm()
        await self._wait_for_vm(
            _VM_RUNNING, self._timings.vm_start_confirm_seconds, ActionOutcome.VM_START_TIMEOUT
        )

    async def _wait_for_vm(
        self, states: frozenset[str], seconds: float, on_timeout: ActionOutcome
    ) -> None:
        deadline = self._monotonic() + seconds
        while True:
            try:
                state = await self._control.vm_state()
            except ActionError as failure:
                if failure.outcome not in _TRANSIENT:
                    raise
            else:
                if state in states:
                    return
            if self._monotonic() >= deadline:
                raise ActionError(on_timeout)
            await self._sleep(self._timings.poll_seconds)

    async def _stop_vm(self, run: _Run) -> None:
        if await self._control.vm_state() in _VM_OFF:
            return
        run.step = ActionStep.STOPPING_VM
        await self._control.shutdown_vm()
        self._stop_sent_at = self._clock()
        await self._wait_for_vm(
            _VM_OFF,
            self._shutdown_timeout.total_seconds(),
            ActionOutcome.VM_STOP_TIMEOUT,
        )
        self._stop_sent_at = None

    async def _force_stop_vm(self, run: _Run) -> None:
        if await self._control.vm_state() not in _VM_OFF:
            run.step = ActionStep.FORCING_VM
            await self._control.force_stop_vm()
            await self._wait_for_vm(
                _VM_OFF, self._timings.force_confirm_seconds, ActionOutcome.VM_STOP_TIMEOUT
            )
        self._stop_sent_at = None

    async def _shutdown_target(self, run: _Run) -> None:
        await self._ensure_no_other_vms()
        if await self._control.vm_state() not in _VM_OFF:
            # „VM zuerst herunterfahren?“ (Entscheidung E10): Bleibt sie an, bricht der Ablauf ab.
            async with self._part(run, ActionName.VM_STOP):
                await self._stop_vm(run)
        await self._power_off(run)

    async def _shutdown_target_force_vm(self, run: _Run) -> None:
        """Nach ausdrücklicher Bestätigung: VM erzwingen, danach Groß sauber herunterfahren.

        Ist die VM nach dem Erzwingen nicht aus, bricht der Ablauf ab und Groß bleibt an.
        Läuft eine weitere VM, wird die Gaming-VM gar nicht erst erzwungen.
        """
        await self._ensure_no_other_vms()
        async with self._part(run, ActionName.VM_FORCE_STOP):
            await self._force_stop_vm(run)
        async with self._part(run, ActionName.SHUTDOWN_TARGET):
            await self._power_off(run)

    async def _ensure_no_other_vms(self) -> None:
        """Groß nie ausschalten, solange weitere VMs laufen (Konzept 4.2, ab v0.4.2).

        Ist die Liste nicht lesbar, bricht der Ablauf mit der Ursache aus vm-watch ab.
        """
        if await self._control.other_running_vms():
            raise ActionError(ActionOutcome.OTHER_VMS_RUNNING)

    async def _power_off(self, run: _Run) -> None:
        run.step = ActionStep.SHUTTING_DOWN
        # Unmittelbar vor dem Ausschalten erneut prüfen, auch nach dem Herunterfahren der VM.
        # guard.sh prüft danach selbst noch einmal (Konzept 3.5).
        await self._ensure_no_other_vms()
        await self._control.power_off()
        deadline = self._monotonic() + self._timings.shutdown_wait_seconds
        while await self._control.target_reachable():
            if self._monotonic() >= deadline:
                raise ActionError(ActionOutcome.SHUTDOWN_TIMEOUT)
            await self._sleep(self._timings.poll_seconds)


def _first_step(name: ActionName) -> ActionStep:
    return {
        ActionName.WAKE_TARGET: ActionStep.WAKING,
        ActionName.VM_START: ActionStep.WAITING_FOR_VM_SERVICE,
        ActionName.VM_STOP: ActionStep.STOPPING_VM,
        ActionName.VM_FORCE_STOP: ActionStep.FORCING_VM,
        ActionName.SHUTDOWN_TARGET: ActionStep.SHUTTING_DOWN,
        ActionName.SHUTDOWN_TARGET_FORCE_VM: ActionStep.FORCING_VM,
    }[name]
