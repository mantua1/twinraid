"""Simulierter Status (Konzept 6.4).

Durchläuft einen festen Ablauf, der jeden Zustand, jede Ursache und jeden Hinweis
mindestens einmal zeigt. Ein Schritt dauert 20 Sekunden; der Worker prüft alle 10 Sekunden.
Bei Fehlern der Statusverbindung nennt er auch den nächsten Versuch (ab v0.4.4).
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from twinraid.integrations.vm_watch import PERMANENT_REASONS, WatchTimings
from twinraid.shared.status import (
    ResourceInfo,
    ResourceState,
    ResourceStatus,
    StatusNotice,
    StatusReason,
    WatchRetry,
)

STEP_SECONDS = 20.0

_ON = ResourceInfo(state=ResourceState.ONLINE)
_OFF = ResourceInfo(state=ResourceState.OFFLINE)
_VM_TARGET_OFF = ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.TARGET_OFFLINE)
_NONE: tuple[StatusNotice, ...] = ()
# Ursachen, nach denen die Sitzung vm-watch kurz wartet und es dann erneut versucht.
TRANSIENT_REASONS = frozenset(
    {StatusReason.WATCH_INTERRUPTED, StatusReason.SSH_UNREACHABLE, StatusReason.SSH_FAILED}
)
_PAUSE = timedelta(seconds=WatchTimings().permanent_pause_seconds)


def _vm(reason: StatusReason) -> ResourceInfo:
    return ResourceInfo(state=ResourceState.UNKNOWN, reason=reason)


def _unknown(reason: StatusReason) -> tuple[ResourceInfo, ResourceInfo, ResourceInfo, tuple[()]]:
    return (_ON, _ON, _vm(reason), ())


# (Klein, Groß, VM, Hinweise) je Schritt.
SCENARIO: tuple[tuple[ResourceInfo, ResourceInfo, ResourceInfo, tuple[StatusNotice, ...]], ...] = (
    (
        ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.RUNS_HERE),
        ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.UNREACHABLE),
        _VM_TARGET_OFF,
        _NONE,
    ),
    (
        _ON,
        ResourceInfo(state=ResourceState.STARTING),
        ResourceInfo(state=ResourceState.UNKNOWN, reason=StatusReason.TARGET_STARTING),
        _NONE,
    ),
    _unknown(StatusReason.WATCH_CONNECTING),
    _unknown(StatusReason.VM_SERVICE_UNAVAILABLE),
    (_ON, _ON, _OFF, _NONE),
    (_ON, _ON, ResourceInfo(state=ResourceState.STARTING), _NONE),
    (_ON, _ON, _ON, _NONE),
    (
        _ON,
        _ON,
        ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_PAUSED),
        (StatusNotice.TARGET_CONNECT_PLUGIN, StatusNotice.TARGET_FLASH_BACKUP),
    ),
    (
        _ON,
        _ON,
        ResourceInfo(state=ResourceState.ONLINE, reason=StatusReason.VM_SUSPENDED),
        (StatusNotice.VM_NAME_MISMATCH,),
    ),
    (_ON, _ON, ResourceInfo(state=ResourceState.STOPPING), _NONE),
    (_ON, _ON, ResourceInfo(state=ResourceState.OFFLINE, reason=StatusReason.VM_CRASHED), _NONE),
    _unknown(StatusReason.WATCH_INTERRUPTED),
    _unknown(StatusReason.SSH_KEY_MISSING),
    _unknown(StatusReason.SSH_HOST_UNTRUSTED),
    _unknown(StatusReason.SSH_HOST_KEY_MISMATCH),
    _unknown(StatusReason.SSH_AUTH_FAILED),
    _unknown(StatusReason.SSH_UNREACHABLE),
    _unknown(StatusReason.SSH_DENIED),
    _unknown(StatusReason.SSH_FAILED),
    _unknown(StatusReason.GUARD_OUTDATED),
    _unknown(StatusReason.VM_NOT_CONFIGURED),
    _unknown(StatusReason.VM_NOT_FOUND),
    (_ON, ResourceInfo(state=ResourceState.STOPPING), _VM_TARGET_OFF, _NONE),
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
        self._started_at = clock().replace(microsecond=0)

    def _steps(self) -> int:
        return int((self._monotonic() - self._started) // STEP_SECONDS)

    def step(self) -> int:
        return self._steps() % len(SCENARIO)

    def _retry(self, reason: StatusReason | None) -> WatchRetry | None:
        """Nächster Versuch: nach einer Pause in 30 Minuten, sonst mit dem nächsten Schritt."""
        step_started = self._started_at + timedelta(seconds=self._steps() * STEP_SECONDS)
        if reason in PERMANENT_REASONS:
            return WatchRetry(at=step_started + _PAUSE, paused=True)
        if reason in TRANSIENT_REASONS:
            return WatchRetry(at=step_started + timedelta(seconds=STEP_SECONDS), paused=False)
        return None

    async def get_status(self) -> ResourceStatus:
        source, target, vm, notices = SCENARIO[self.step()]
        return ResourceStatus(
            source=source,
            target=target,
            vm=vm,
            checked_at=self._clock(),
            notices=notices,
            watch_retry=self._retry(vm.reason),
        )
