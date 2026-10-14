"""Simulierter Status (Konzept 6.4).

Durchläuft einen festen Ablauf, der jeden Zustand und jede Ursache mindestens
einmal zeigt. Ein Schritt dauert 20 Sekunden; der Worker prüft alle 10 Sekunden.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime

from twinraid.shared.status import ResourceInfo, ResourceState, ResourceStatus, StatusReason

STEP_SECONDS = 20.0

_ON = ResourceInfo(state=ResourceState.ONLINE)
_OFF = ResourceInfo(state=ResourceState.OFFLINE)
_UNREACHABLE = ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.UNREACHABLE)
_VM_TARGET_OFF = ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.TARGET_OFFLINE)


def _vm(reason: StatusReason) -> ResourceInfo:
    return ResourceInfo(state=ResourceState.UNKNOWN, reason=reason)


# (Klein, Groß, VM) je Schritt.
SCENARIO: tuple[tuple[ResourceInfo, ResourceInfo, ResourceInfo], ...] = (
    (
        ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.RUNS_HERE),
        ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.UNREACHABLE),
        _VM_TARGET_OFF,
    ),
    (_ON, ResourceInfo(state=ResourceState.STARTING), _VM_TARGET_OFF),
    (_ON, _ON, _OFF),
    (_ON, _ON, ResourceInfo(state=ResourceState.STARTING)),
    (_ON, _ON, _ON),
    (_ON, _ON, ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_PAUSED)),
    (_ON, _ON, ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_SUSPENDED)),
    (_ON, _ON, ResourceInfo(state=ResourceState.STOPPING)),
    (_ON, _ON, ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.VM_CRASHED)),
    (_ON, _ON, _vm(StatusReason.API_UNREACHABLE)),
    (_ON, _ON, _vm(StatusReason.API_KEY_INVALID)),
    (_ON, _ON, _vm(StatusReason.API_FORBIDDEN)),
    (_ON, _ON, _vm(StatusReason.API_CERT_CHANGED)),
    (_ON, _ON, _vm(StatusReason.API_CERT_UNTRUSTED)),
    (_ON, _ON, _vm(StatusReason.API_KEY_MISSING)),
    (_ON, _ON, _vm(StatusReason.API_REDIRECTED)),
    (_ON, _ON, _vm(StatusReason.API_ERROR)),
    (_ON, _ON, _vm(StatusReason.VM_NOT_FOUND)),
    (_ON, ResourceInfo(state=ResourceState.STOPPING), _VM_TARGET_OFF),
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MockStatusProvider:
    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._monotonic = monotonic
        self._clock = clock
        self._started = monotonic()

    def step(self) -> int:
        return int((self._monotonic() - self._started) // STEP_SECONDS) % len(SCENARIO)

    async def get_status(self) -> ResourceStatus:
        source, target, vm = SCENARIO[self.step()]
        return ResourceStatus(source=source, target=target, vm=vm, checked_at=self._clock())
