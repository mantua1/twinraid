"""Statusabfragen (Konzept 3.4, 3.5 und 4.1): TCP-Check von Groß, VM über die Sitzung vm-watch.

Klein wird nicht geprüft: TwinRaid läuft dort, und ohne „Host access to custom
networks“ erreicht der Container seinen eigenen Host nicht (Konzept 5.1).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.vm_watch import WatchState, WatchView
from twinraid.shared.status import (
    ResourceInfo,
    ResourceState,
    ResourceStatus,
    StatusNotice,
    StatusReason,
)


class StatusProvider(Protocol):
    async def get_status(self) -> ResourceStatus: ...


TcpCheck = Callable[[str, int], Awaitable[bool]]

# libvirt-Zustände (wie bei der Unraid-API) und ihre Anzeige.
_VM_STATES: dict[str, ResourceInfo] = {
    "RUNNING": ResourceInfo(state=ResourceState.ONLINE),
    "IDLE": ResourceInfo(state=ResourceState.ONLINE),
    "PAUSED": ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_PAUSED),
    "PMSUSPENDED": ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_SUSPENDED),
    "SHUTDOWN": ResourceInfo(state=ResourceState.STOPPING),
    "SHUTOFF": ResourceInfo(state=ResourceState.OFFLINE),
    "CRASHED": ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.VM_CRASHED),
    # Meldungen von guard.sh (Konzept 3.5)
    "NOT_CONFIGURED": ResourceInfo(
        state=ResourceState.UNKNOWN, reason=StatusReason.VM_NOT_CONFIGURED
    ),
    "NOT_FOUND": ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.VM_NOT_FOUND),
    "LIBVIRT_DOWN": ResourceInfo(
        state=ResourceState.UNKNOWN, reason=StatusReason.VM_SERVICE_UNAVAILABLE
    ),
    "UNKNOWN": ResourceInfo(
        state=ResourceState.UNKNOWN, reason=StatusReason.VM_SERVICE_UNAVAILABLE
    ),
}


# Klein ist online, solange der Worker antwortet.
SOURCE_STATUS = ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.RUNS_HERE)
VM_TARGET_OFFLINE = ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.TARGET_OFFLINE)


def map_vm_state(state: str) -> ResourceInfo:
    """Übersetzt den Zustand; NOSTATE und Unbekanntes werden zu „Unbekannt“."""
    return _VM_STATES.get(state, ResourceInfo(state=ResourceState.UNKNOWN))


def server_status(reachable: bool) -> ResourceInfo:
    if reachable:
        return ResourceInfo(state=ResourceState.ONLINE)
    return ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.UNREACHABLE)


def vm_status(
    view: WatchView, expected_name: str | None
) -> tuple[ResourceInfo, tuple[str, ...] | None, tuple[StatusNotice, ...]]:
    """VM, weitere laufende VMs und Hinweise aus dem Stand der Sitzung (Groß ist online)."""
    report = view.report
    if report is None:
        return ResourceInfo(state=ResourceState.UNKNOWN, reason=view.reason), None, ()
    notices: list[StatusNotice] = []
    if report.connect_plugin:
        notices.append(StatusNotice.TARGET_CONNECT_PLUGIN)
    if report.flash_backup == "active":
        notices.append(StatusNotice.TARGET_FLASH_BACKUP)
    if expected_name is not None and report.name is not None and report.name != expected_name:
        notices.append(StatusNotice.VM_NAME_MISMATCH)
    return map_vm_state(report.vm), report.others, tuple(notices)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RealStatusProvider:
    """Echte Abfragen. Nur lesend: TCP-Verbindungsaufbau zu Groß und der Stand von vm-watch."""

    def __init__(
        self,
        config: TwinRaidConfig,
        watch: WatchState,
        tcp_check: TcpCheck = tcp_reachable,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._watch = watch
        self._tcp_check = tcp_check
        self._clock = clock

    async def get_status(self) -> ResourceStatus:
        target = self._config.servers.target
        target_up = await self._tcp_check(str(target.host), target.webgui.effective_port)
        self._watch.set_target_online(target_up)
        if target_up:
            vm, others, notices = vm_status(
                self._watch.view(), self._config.gaming_vm.unraid_vm_name
            )
        else:
            vm, others, notices = VM_TARGET_OFFLINE, None, ()
        return ResourceStatus(
            source=SOURCE_STATUS,
            target=server_status(target_up),
            vm=vm,
            checked_at=self._clock(),
            other_vms_running=others,
            notices=notices,
        )
