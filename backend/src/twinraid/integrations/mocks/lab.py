"""Interaktive Simulation von Unraid Groß und der Gaming-VM (Konzept 6.4, Entscheidung E11).

Groß und die VM starten ausgeschaltet. Aktionen wirken mit festen Wartezeiten.
Mit twinraid-admin mock-fault lassen sich Fehler einschalten, mit mock-tour
der feste Rundlauf durch alle Zustände (MockStatusProvider).

Die Schalter sind Dateien neben dem Worker-Socket. Nur root kann sie anlegen;
der Web-Prozess kann sie nicht umlegen.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from twinraid.integrations.control import ActionError
from twinraid.integrations.mocks.status import MockStatusProvider
from twinraid.integrations.status import (
    SOURCE_STATUS,
    VM_TARGET_OFFLINE,
    map_vm_state,
    server_status,
)
from twinraid.integrations.vm_watch import PERMANENT_REASONS, WatchTimings
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import (
    ResourceInfo,
    ResourceState,
    ResourceStatus,
    StatusNotice,
    StatusReason,
    WatchRetry,
)

# Simulierte Fehler (twinraid-admin mock-fault <Name> on|off).
FAULTS = (
    "vm-hang",
    "ssh-denied",
    "no-boot",
    "other-vm",
    "vm-not-configured",
    "libvirt-down",
    "guard-outdated",
    "connect-plugin",
    "status-key-rejected",
)
# Name der weiteren VM bei mock-fault other-vm.
MOCK_OTHER_VM = "Test-VM"
_OFF_STATES = frozenset({"SHUTOFF", "CRASHED"})
# Pause nach einem dauerhaften Fehler der Statusverbindung, wie im Modus real (Konzept 3.5).
PERMANENT_PAUSE = timedelta(seconds=WatchTimings().permanent_pause_seconds)


def fault_flag(directory: Path, name: str) -> Path:
    if name not in FAULTS:
        raise ValueError(f"unknown fault {name!r}")
    return directory / f"mock-fault-{name}"


def tour_flag(directory: Path) -> Path:
    return directory / "mock-tour"


def set_flag(path: Path, on: bool) -> None:
    if on:
        path.touch(mode=0o600)
    else:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class LabTimings:
    boot_seconds: float = 20.0
    # Danach antwortet der VM-Dienst über vm-watch.
    vm_service_seconds: float = 10.0
    vm_start_seconds: float = 3.0
    vm_stop_seconds: float = 8.0
    shutdown_seconds: float = 15.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MockLab:
    """Simuliertes Groß: Status für den Monitor und ActionControl für die Abläufe."""

    def __init__(
        self,
        flag_dir: Path,
        timings: LabTimings | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._flag_dir = flag_dir
        self._timings = timings or LabTimings()
        self._monotonic = monotonic
        self._clock = clock
        self._tour = MockStatusProvider(monotonic=monotonic, clock=clock)
        # Groß: off, booting, on, shutting_down; seit wann.
        self._power = "off"
        self._since = monotonic()
        self._vm_state = "SHUTOFF"
        # Geplanter Wechsel der VM (Ziel, Zeitpunkt).
        self._vm_change: tuple[str, float] | None = None
        # Ende der Pause nach einem dauerhaften Fehler der Statusverbindung.
        self._pause_until: datetime | None = None

    def fault(self, name: str) -> bool:
        return fault_flag(self._flag_dir, name).exists()

    def tour_active(self) -> bool:
        return tour_flag(self._flag_dir).exists()

    def _advance(self) -> None:
        now = self._monotonic()
        timings = self._timings
        if self._power == "booting" and now >= self._since + timings.boot_seconds:
            self._power, self._since = "on", self._since + timings.boot_seconds
        if self._power == "shutting_down" and now >= self._since + timings.shutdown_seconds:
            self._power, self._since = "off", now
            self._vm_state, self._vm_change = "SHUTOFF", None
        if self._vm_change is not None and now >= self._vm_change[1]:
            self._vm_state, self._vm_change = self._vm_change[0], None

    def _target_up(self) -> bool:
        self._advance()
        return self._power in ("on", "shutting_down")

    def _vm_service_ready(self) -> bool:
        if not self._target_up() or self.fault("libvirt-down"):
            return False
        if self._power == "shutting_down":
            return True
        return self._monotonic() >= self._since + self._timings.vm_service_seconds

    def _problem(self) -> StatusReason | None:
        """Warum vm-watch gerade keinen Zustand der VM liefert (Groß ist an)."""
        # Groß lehnt den Key „status“ ab: Die Anmeldung scheitert vor guard.sh.
        if self.fault("status-key-rejected"):
            return StatusReason.SSH_AUTH_FAILED
        if self.fault("guard-outdated"):
            return StatusReason.GUARD_OUTDATED
        if self.fault("vm-not-configured"):
            return StatusReason.VM_NOT_CONFIGURED
        if not self._vm_service_ready():
            return StatusReason.VM_SERVICE_UNAVAILABLE
        return None

    # --- Status (wie RealStatusProvider) ---

    async def get_status(self) -> ResourceStatus:
        if self.tour_active():
            return await self._tour.get_status()
        others: tuple[str, ...] | None = None
        notices: tuple[StatusNotice, ...] = ()
        problem: StatusReason | None = None
        up = self._target_up()
        if not up:
            vm = VM_TARGET_OFFLINE
        elif (problem := self._problem()) is not None:
            vm = ResourceInfo(state=ResourceState.UNKNOWN, reason=problem)
        else:
            vm = map_vm_state(self._vm_state)
            others = self._others()
        if up and self.fault("connect-plugin"):
            notices = (StatusNotice.TARGET_CONNECT_PLUGIN, StatusNotice.TARGET_FLASH_BACKUP)
        return ResourceStatus(
            source=SOURCE_STATUS,
            target=server_status(up),
            vm=vm,
            checked_at=self._clock(),
            other_vms_running=others,
            notices=notices,
            watch_retry=self._retry(problem),
        )

    def _retry(self, problem: StatusReason | None) -> WatchRetry | None:
        """Pause wie die Sitzung vm-watch: 30 Minuten, dann ein neuer Versuch (Konzept 3.5).

        Den Fehler auszuschalten wirkt wie ein erfolgreiches trust-host-key: Die Pause endet.
        """
        if problem not in PERMANENT_REASONS:
            self._pause_until = None
            return None
        now = self._clock()
        if self._pause_until is None or now >= self._pause_until:
            self._pause_until = now.replace(microsecond=0) + PERMANENT_PAUSE
        return WatchRetry(at=self._pause_until, paused=True)

    def _others(self) -> tuple[str, ...]:
        return (MOCK_OTHER_VM,) if self.fault("other-vm") else ()

    def _require_state(self) -> None:
        if not self._target_up():
            raise ActionError(ActionOutcome.WATCH_INTERRUPTED)
        problem = self._problem()
        if problem is not None:
            raise ActionError(ActionOutcome(problem.value))

    def _require_guard(self) -> None:
        """Befehle mit dem Key „power“: ein veraltetes guard.sh lehnt sie ab."""
        if self.fault("ssh-denied") or self.fault("guard-outdated"):
            raise ActionError(ActionOutcome.SSH_DENIED)

    # --- ActionControl ---

    async def target_reachable(self) -> bool:
        return self._target_up()

    async def vm_state(self) -> str:
        self._require_state()
        return self._vm_state

    async def wake(self) -> None:
        # Das Magic Packet ist „gesendet“; bei no-boot reagiert Groß nicht.
        if self._power == "off" and not self.fault("no-boot"):
            self._power, self._since = "booting", self._monotonic()

    async def start_vm(self) -> None:
        self._require_guard()
        if self.fault("vm-not-configured"):
            raise ActionError(ActionOutcome.VM_NOT_CONFIGURED)
        if self._vm_state not in _OFF_STATES:
            raise ActionError(ActionOutcome.REMOTE_FAILED)
        self._vm_change = ("RUNNING", self._monotonic() + self._timings.vm_start_seconds)

    async def shutdown_vm(self) -> None:
        self._require_guard()
        if self.fault("vm-not-configured"):
            raise ActionError(ActionOutcome.VM_NOT_CONFIGURED)
        if self.fault("vm-hang") or self._vm_state == "SHUTOFF":
            return
        self._vm_change = ("SHUTOFF", self._monotonic() + self._timings.vm_stop_seconds)

    async def force_stop_vm(self) -> None:
        self._require_guard()
        if self.fault("vm-not-configured"):
            raise ActionError(ActionOutcome.VM_NOT_CONFIGURED)
        self._vm_state, self._vm_change = "SHUTOFF", None

    async def other_running_vms(self) -> tuple[str, ...]:
        self._require_state()
        return self._others()

    async def power_off(self) -> None:
        self._require_guard()
        # Wie guard.sh: nur ausschalten, wenn keine VM läuft (Konzept 3.5).
        if self._target_up():
            if self.fault("libvirt-down"):
                raise ActionError(ActionOutcome.VM_SERVICE_UNAVAILABLE)
            if self._vm_state not in _OFF_STATES:
                raise ActionError(ActionOutcome.VM_RUNNING)
            if self._others():
                raise ActionError(ActionOutcome.OTHER_VMS_RUNNING)
            self._power, self._since = "shutting_down", self._monotonic()
