"""Aktionen mit Außenwirkung auf Unraid Groß (Konzept 3.4 und 4.2).

ActionControl ist die Schnittstelle, die der Worker für seine Abläufe nutzt.
RealControl setzt sie mit TCP-Check, Wake-on-LAN und SSH über guard.sh um; den
Zustand der VM liest es nur aus der Sitzung vm-watch (Konzept 3.5). Die
Simulation steht in integrations/mocks/lab.py.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.network import tcp_reachable
from twinraid.integrations.ssh import SshError, SshPower
from twinraid.integrations.vm_watch import WatchReport, WatchState, watch_outcome
from twinraid.integrations.wol import WolTarget, send_magic_packet
from twinraid.shared.actions import ActionOutcome


class ActionError(Exception):
    """Ein Schritt einer Aktion ist gescheitert; outcome stammt aus der festen Liste."""

    def __init__(self, outcome: ActionOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


class ActionControl(Protocol):
    async def target_reachable(self) -> bool: ...

    async def vm_state(self) -> str:
        """Zustand der Gaming-VM (libvirt, z. B. RUNNING). Wirft ActionError."""
        ...

    async def wake(self) -> None: ...

    async def start_vm(self) -> None: ...

    async def shutdown_vm(self) -> None:
        """Nur das ACPI-Signal, nie erzwingen (Entscheidung E8)."""
        ...

    async def force_stop_vm(self) -> None: ...

    async def other_running_vms(self) -> tuple[str, ...]:
        """Laufende VMs außer der Gaming-VM (Konzept 4.2). Wirft ActionError."""
        ...

    async def power_off(self) -> None: ...


# Meldungen von guard.sh, die einen Ablauf aufhalten (Konzept 3.5).
_STATE_OUTCOMES: dict[str, ActionOutcome] = {
    "NOT_CONFIGURED": ActionOutcome.VM_NOT_CONFIGURED,
    "NOT_FOUND": ActionOutcome.VM_NOT_FOUND,
    "LIBVIRT_DOWN": ActionOutcome.VM_SERVICE_UNAVAILABLE,
    "UNKNOWN": ActionOutcome.VM_SERVICE_UNAVAILABLE,
}

TcpCheck = Callable[[str, int], Awaitable[bool]]


class RealControl:
    def __init__(
        self,
        config: TwinRaidConfig,
        watch: WatchState,
        power: SshPower,
        wol: WolTarget | None,
        tcp_check: TcpCheck = tcp_reachable,
        send_wol: Callable[[WolTarget], None] = send_magic_packet,
    ) -> None:
        self._config = config
        self._watch = watch
        self._power = power
        self._wol = wol
        self._tcp_check = tcp_check
        self._send_wol = send_wol

    async def target_reachable(self) -> bool:
        target = self._config.servers.target
        reachable = await self._tcp_check(str(target.host), target.webgui.effective_port)
        # Die Sitzung vm-watch folgt jeder Prüfung, auch während eines Ablaufs.
        self._watch.set_target_online(reachable)
        return reachable

    def _report(self) -> WatchReport:
        view = self._watch.view()
        if view.report is None:
            raise ActionError(watch_outcome(view.reason))
        return view.report

    async def vm_state(self) -> str:
        state = self._report().vm
        problem = _STATE_OUTCOMES.get(state)
        if problem is not None:
            raise ActionError(problem)
        return state

    async def wake(self) -> None:
        if self._wol is None:
            # Ohne Broadcast-Adresse ist Wecken gar nicht freigeschaltet (Worker-Start).
            raise ActionError(ActionOutcome.WOL_FAILED)
        try:
            await asyncio.to_thread(self._send_wol, self._wol)
        except OSError:
            raise ActionError(ActionOutcome.WOL_FAILED) from None

    async def start_vm(self) -> None:
        await self._guard(self._power.vm_start())

    async def shutdown_vm(self) -> None:
        await self._guard(self._power.vm_shutdown())

    async def force_stop_vm(self) -> None:
        await self._guard(self._power.vm_force_stop())

    async def other_running_vms(self) -> tuple[str, ...]:
        report = self._report()
        if report.others is None:
            raise ActionError(_STATE_OUTCOMES.get(report.vm, ActionOutcome.VM_SERVICE_UNAVAILABLE))
        return report.others

    async def power_off(self) -> None:
        try:
            await self._power.poweroff()
        except SshError as exc:
            # Groß beendet die Verbindung, während es herunterfährt: Das zählt als gesendet.
            if not exc.connection_closed:
                raise ActionError(exc.outcome) from None

    @staticmethod
    async def _guard(request: Awaitable[None]) -> None:
        try:
            await request
        except SshError as exc:
            raise ActionError(exc.outcome) from None
