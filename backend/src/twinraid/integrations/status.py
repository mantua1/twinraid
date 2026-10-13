"""Statusabfragen (Konzept 3.4, 3.5 und 4.1): TCP-Checks beider Server, VM über die Unraid-API."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.unraid_api import UnraidApiError
from twinraid.shared.status import (
    ResourceInfo,
    ResourceState,
    ResourceStatus,
    StatusReason,
)


class StatusProvider(Protocol):
    async def get_status(self) -> ResourceStatus: ...


class VmStateSource(Protocol):
    async def vm_state(self, vm_name: str) -> str | None: ...


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
    """Echte Abfragen. Nur lesend: TCP-Verbindungsaufbau und eine GraphQL-Abfrage."""

    def __init__(
        self,
        config: TwinRaidConfig,
        api: VmStateSource,
        tcp_check: TcpCheck = tcp_reachable,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._api = api
        self._tcp_check = tcp_check
        self._clock = clock

    async def get_status(self) -> ResourceStatus:
        source, target = self._config.servers.source, self._config.servers.target
        source_up, target_up = await asyncio.gather(
            self._tcp_check(str(source.host), source.webgui.effective_port),
            self._tcp_check(str(target.host), target.webgui.effective_port),
        )
        return ResourceStatus(
            source=_server(source_up),
            target=_server(target_up),
            vm=await self._vm(target_up),
            checked_at=self._clock(),
        )

    async def _vm(self, target_up: bool) -> ResourceInfo:
        if not target_up:
            return ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.TARGET_OFFLINE)
        try:
            state = await self._api.vm_state(self._config.gaming_vm.unraid_vm_name)
        except UnraidApiError as exc:
            return ResourceInfo(state=ResourceState.UNKNOWN, reason=exc.reason)
        if state is None:
            return ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.VM_NOT_FOUND)
        return map_vm_state(state)
