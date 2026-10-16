"""Statusabfragen (Konzept 3.4, 3.5 und 4.1): TCP-Check von Groß, VM über die Unraid-API.

Klein wird nicht geprüft: TwinRaid läuft dort, und ohne „Host access to custom
networks“ erreicht der Container seinen eigenen Host nicht (Konzept 5.1).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.unraid_api import UnraidApiError
from twinraid.integrations.vm_list import other_running_vms
from twinraid.shared.status import (
    ResourceInfo,
    ResourceState,
    ResourceStatus,
    StatusReason,
)


class StatusProvider(Protocol):
    async def get_status(self) -> ResourceStatus: ...


class VmListSource(Protocol):
    async def domain_states(self) -> dict[str, str]:
        """Name und libvirt-Zustand aller VMs auf Groß."""
        ...


TcpCheck = Callable[[str, int], Awaitable[bool]]

# libvirt-Zustände aus der Unraid-API (VmState) und ihre Anzeige.
_VM_STATES: dict[str, ResourceInfo] = {
    "RUNNING": ResourceInfo(state=ResourceState.ONLINE),
    "IDLE": ResourceInfo(state=ResourceState.ONLINE),
    "PAUSED": ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_PAUSED),
    "PMSUSPENDED": ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_SUSPENDED),
    "SHUTDOWN": ResourceInfo(state=ResourceState.STOPPING),
    "SHUTOFF": ResourceInfo(state=ResourceState.OFFLINE),
    "CRASHED": ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.VM_CRASHED),
}


# Klein ist online, solange der Worker antwortet.
SOURCE_STATUS = ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.RUNS_HERE)


def map_vm_state(state: str) -> ResourceInfo:
    """Übersetzt den libvirt-Zustand; NOSTATE und Unbekanntes werden zu „Unbekannt“."""
    return _VM_STATES.get(state, ResourceInfo(state=ResourceState.UNKNOWN))


def _server(reachable: bool) -> ResourceInfo:
    if reachable:
        return ResourceInfo(state=ResourceState.ONLINE)
    return ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.UNREACHABLE)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RealStatusProvider:
    """Echte Abfragen. Nur lesend: TCP-Verbindungsaufbau zu Groß und eine GraphQL-Abfrage."""

    def __init__(
        self,
        config: TwinRaidConfig,
        api: VmListSource,
        tcp_check: TcpCheck = tcp_reachable,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._api = api
        self._tcp_check = tcp_check
        self._clock = clock

    async def get_status(self) -> ResourceStatus:
        target = self._config.servers.target
        target_up = await self._tcp_check(str(target.host), target.webgui.effective_port)
        vm, others = await self._vms(target_up)
        return ResourceStatus(
            source=SOURCE_STATUS,
            target=_server(target_up),
            vm=vm,
            checked_at=self._clock(),
            other_vms_running=others,
        )

    async def _vms(self, target_up: bool) -> tuple[ResourceInfo, tuple[str, ...] | None]:
        """Zustand der Gaming-VM und die weiteren laufenden VMs aus einer Abfrage."""
        if not target_up:
            off = ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.TARGET_OFFLINE)
            return off, None
        try:
            states = await self._api.domain_states()
        except UnraidApiError as exc:
            return ResourceInfo(state=ResourceState.UNKNOWN, reason=exc.reason), None
        own = self._config.gaming_vm.unraid_vm_name
        others = other_running_vms(states, own)
        state = states.get(own)
        if state is None:
            not_found = ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.VM_NOT_FOUND)
            return not_found, others
        return map_vm_state(state), others
