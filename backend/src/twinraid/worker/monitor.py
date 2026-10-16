"""Regelmäßige Statusprüfung im Worker (Konzept 4.1).

Prüft alle 10 Sekunden und merkt sich das Ergebnis. get_status liefert
sofort den zuletzt geprüften Stand, ohne selbst Server anzusprechen. Meldet
die Sitzung vm-watch eine Änderung, prüft der Monitor sofort (Konzept 3.5).
"""

import asyncio
import contextlib
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
        changes: asyncio.Event | None = None,
    ) -> None:
        self._provider = provider
        self._interval = interval
        self._clock = clock
        self._changes = changes
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
            if self._changes is not None:
                self._changes.clear()
            await self.check_once()
            await self._wait()

    async def _wait(self) -> None:
        """Bis zur nächsten regulären Prüfung, oder früher bei einer gemeldeten Änderung."""
        if self._changes is None:
            await asyncio.sleep(self._interval)
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._interval):
                await self._changes.wait()

    @staticmethod
    def _log_changes(before: ResourceStatus, after: ResourceStatus) -> None:
        # Nur Zustand und Ursache aus festen Listen; nie Antworttexte oder Secrets.
        for name in _RESOURCES:
            old: ResourceInfo = getattr(before, name)
            new: ResourceInfo = getattr(after, name)
            if old != new:
                logger.info("status %s: %s -> %s", name, _describe(old), _describe(new))
        if before.notices != after.notices:
            notices = ", ".join(notice.value for notice in after.notices) or "none"
            logger.info("status notices: %s", notices)
