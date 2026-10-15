"""Aktionen mit Außenwirkung auf Unraid Groß (Konzept 3.4 und 4.2).

ActionControl ist die Schnittstelle, die der Worker für seine Abläufe nutzt.
RealControl setzt sie mit TCP-Check, Wake-on-LAN, Unraid-API und SSH um; die
Simulation steht in integrations/mocks/lab.py.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.ssh import SshError, SshPower
from twinraid.integrations.unraid_api import UnraidApi, UnraidApiError, VmDomain
from twinraid.integrations.wol import WolTarget, send_magic_packet
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import StatusReason


class ActionError(Exception):
    """Ein Schritt einer Aktion ist gescheitert; outcome stammt aus der festen Liste."""

    def __init__(self, outcome: ActionOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


class ActionControl(Protocol):
    async def target_reachable(self) -> bool: ...

    async def find_vm(self) -> VmDomain:
        """Die Gaming-VM laut API. Wirft ActionError (z. B. vm_not_found)."""
        ...

    async def wake(self) -> None: ...

    async def start_vm(self, vm: VmDomain) -> None: ...

    async def shutdown_vm(self, vm: VmDomain) -> None:
        """Nur das ACPI-Signal, nie erzwingen (Entscheidung E8)."""
        ...

    async def force_stop_vm(self, vm: VmDomain) -> None: ...

    async def power_off(self) -> None: ...


def api_outcome(reason: StatusReason) -> ActionOutcome:
    """Die Ursachen der API heißen in Status und Aktionslog gleich."""
    try:
        return ActionOutcome(reason.value)
    except ValueError:
        return ActionOutcome.API_ERROR


TcpCheck = Callable[[str, int], Awaitable[bool]]


class RealControl:
    def __init__(
        self,
        config: TwinRaidConfig,
        api: UnraidApi,
        ssh: SshPower,
        wol: WolTarget | None,
        tcp_check: TcpCheck = tcp_reachable,
        send_wol: Callable[[WolTarget], None] = send_magic_packet,
    ) -> None:
        self._config = config
        self._api = api
        self._ssh = ssh
        self._wol = wol
        self._tcp_check = tcp_check
        self._send_wol = send_wol

    async def target_reachable(self) -> bool:
        target = self._config.servers.target
        return await self._tcp_check(str(target.host), target.webgui.effective_port)

    async def find_vm(self) -> VmDomain:
        try:
            vm = await self._api.find_vm(self._config.gaming_vm.unraid_vm_name)
        except UnraidApiError as exc:
            raise ActionError(api_outcome(exc.reason)) from None
        if vm is None:
            raise ActionError(ActionOutcome.VM_NOT_FOUND)
        return vm

    async def wake(self) -> None:
        if self._wol is None:
            # Ohne Broadcast-Adresse ist Wecken gar nicht freigeschaltet (Worker-Start).
            raise ActionError(ActionOutcome.WOL_FAILED)
        try:
            await asyncio.to_thread(self._send_wol, self._wol)
        except OSError:
            raise ActionError(ActionOutcome.WOL_FAILED) from None

    async def start_vm(self, vm: VmDomain) -> None:
        try:
            await self._api.start_vm(vm)
        except UnraidApiError as exc:
            raise ActionError(api_outcome(exc.reason)) from None

    async def shutdown_vm(self, vm: VmDomain) -> None:
        try:
            await self._ssh.vm_shutdown(vm.uuid)
        except SshError as exc:
            raise ActionError(exc.outcome) from None

    async def force_stop_vm(self, vm: VmDomain) -> None:
        try:
            await self._api.force_stop_vm(vm)
        except UnraidApiError as exc:
            raise ActionError(api_outcome(exc.reason)) from None

    async def power_off(self) -> None:
        try:
            await self._ssh.poweroff()
        except SshError as exc:
            # Groß beendet die Verbindung, während es herunterfährt: Das zählt als gesendet.
            if not exc.connection_closed:
                raise ActionError(exc.outcome) from None
