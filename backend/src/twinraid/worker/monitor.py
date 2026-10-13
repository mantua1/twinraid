"""Regelmäßige Statusprüfung im Worker (Konzept 4.1).

Prüft alle 10 Sekunden und merkt sich das Ergebnis. get_status liefert
sofort den zuletzt geprüften Stand, ohne selbst Server anzusprechen.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from twinraid.integrations.status import StatusProvider
from twinraid.shared.status import NOT_CHECKED, UNKNOWN, ResourceInfo, ResourceStatus

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 10.0
_RESOURCES = ("source", "target", "vm")


def _describe(info: ResourceInfo) -> str:
    return info.state.value if info.reason is None else f"{info.state.value} ({info.reason.value})"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StatusMonitor:
    def __init__(
        self,
        provider: StatusProvider,
        interval: float = CHECK_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._provider = provider
        self._interval = interval
        self._clock = clock
        self._current = NOT_CHECKED

    def current(self) -> ResourceStatus:
        return self._current

    async def check_once(self) -> ResourceStatus:
        try:
            status = await self._provider.get_status()
        except Exception:  # Eine gescheiterte Prüfung darf den Worker nie beenden.
            logger.exception("status check failed")
            status = ResourceStatus(
                source=UNKNOWN, target=UNKNOWN, vm=UNKNOWN, checked_at=self._clock()
            )
        self._log_changes(self._current, status)
        self._current = status
        return status

    async def run(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(self._interval)

    @staticmethod
    def _log_changes(before: ResourceStatus, after: ResourceStatus) -> None:
        # Nur Zustand und Ursache aus festen Listen; nie Antworttexte oder Secrets.
        for name in _RESOURCES:
            old: ResourceInfo = getattr(before, name)
            new: ResourceInfo = getattr(after, name)
            if old != new:
                logger.info("status %s: %s -> %s", name, _describe(old), _describe(new))
