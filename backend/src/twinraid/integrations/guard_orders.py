"""Aufträge an den Wächter auf Klein (Konzept 3.6, „Auftragsnummer und Neustarts“, ab v0.5.1).

Nur der Worker schreibt Aufträge. Für jeden liest er den Status frisch, schreibt genau eine
Zeile mit der nächsten Nummer an Ort und Stelle in die eingebundene Auftragsdatei und
wartet, bis der Wächter sie ausgeführt hat. Was erlaubt ist, entscheidet der Wächter; die
Antwort übernimmt der Worker nur nach dem strengen Schema aus integrations/source_guard.py.
Die Auftragsnummer führt allein der Wächter.
"""

import asyncio
import hashlib
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from twinraid.integrations.source_guard import (
    GuardObservation,
    GuardPaths,
    GuardReport,
    evaluate,
    read_observation,
)
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.source_guard import (
    GuardDetails,
    GuardError,
    GuardOrder,
    GuardOrderResult,
    GuardState,
)

# Eine Zeile: mit Leerzeichen auf 255 Zeichen aufgefüllt, dann "\n" (Konzept 3.6, „Aufträge“).
ORDER_LINE_LENGTH = 255
MAX_SEQ = 999_999_999
# Container-Namen wie im Wächter (Zeichenregel von Docker).
CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class GuardOrderError(Exception):
    """Der Auftrag kam nicht zustande; outcome ist source_guard_inactive oder _lost."""

    def __init__(self, outcome: ActionOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


def order_line(seq: int, order: GuardOrder, container: str | None = None) -> bytes:
    """TRSG1 <Nr> <Auftrag> [<Container>] <Prüfsumme>; nur stop trägt einen Container."""
    if (order is GuardOrder.STOP) != (container is not None):
        raise ValueError("only stop takes a container name, and stop needs one")
    if container is not None and CONTAINER_NAME.fullmatch(container) is None:
        raise ValueError("invalid container name")
    if not 1 <= seq <= MAX_SEQ:
        raise ValueError("sequence number out of range")
    payload = f"TRSG1 {seq} {order.value}"
    if container is not None:
        payload += f" {container}"
    checksum = hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]
    return f"{payload} {checksum}".ljust(ORDER_LINE_LENGTH).encode("ascii") + b"\n"


def write_order(path: Path, line: bytes) -> None:
    """Schreibt an Ort und Stelle: Die Datei ist eingebunden und darf nie ersetzt werden."""
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("the order file is not a regular file")
        written = 0
        while written < len(line):
            written += os.pwrite(fd, line[written:], written)
        os.ftruncate(fd, len(line))
    finally:
        os.close(fd)


class GuardChannel(Protocol):
    async def observe(self) -> GuardReport:
        """Status des Wächters, frisch gelesen (nicht aus dem Takt von 5 s)."""
        ...

    async def order(
        self,
        order: GuardOrder,
        container: str | None = None,
        reply_seconds: float | None = None,
    ) -> GuardOrderResult:
        """Schickt einen Auftrag und wartet auf das Ergebnis. Wirft GuardOrderError.

        reply_seconds ersetzt die übliche Wartezeit von 10 s (bei stop: stop_timeout + 15 s).
        """
        ...

    async def post(self, order: GuardOrder) -> None:
        """Schreibt einen Auftrag, ohne auf das Ergebnis zu warten (Worker endet, 4.3)."""
        ...


@dataclass(frozen=True)
class OrderTimings:
    """Feste Zeiten (Konzept 4.3, „Probelauf in TwinRaid“)."""

    reply_seconds: float = 10.0
    poll_seconds: float = 0.5
    # Höchstens ein Auftrag pro Sekunde (Konzept 3.6).
    gap_seconds: float = 1.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def active_details(report: GuardReport) -> GuardDetails:
    """Details eines aktiven Wächters; sonst source_guard_inactive.

    Hat der Wächter seinen Zustand verloren (state_lost, neue Instanz), ist ein laufender
    Abschnitt verloren: source_guard_lost (Konzept 3.6, „Auftragsnummer und Neustarts“).
    """
    status = report.status
    if status.error is GuardError.STATE_LOST:
        raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
    details = status.details
    if status.state is not GuardState.ACTIVE or details is None:
        raise GuardOrderError(ActionOutcome.SOURCE_GUARD_INACTIVE)
    return details


class RealGuardChannel:
    def __init__(
        self,
        paths: GuardPaths | None = None,
        timings: OrderTimings | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._paths = paths or GuardPaths()
        self._timings = timings or OrderTimings()
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_heartbeat: int | None = None
        self._last_order_at: float | None = None
        # Zuletzt geschriebene Nummer je Instanz, nur im Speicher: Wird ein Auftrag geschrieben,
        # während der Wächter den vorigen noch ausführt (ein abgebrochenes Warten auf stop),
        # steht dessen Nummer noch nicht im Status. Die Nummer führt weiter der Wächter.
        self._written: tuple[str, int] | None = None

    async def observe(self) -> GuardReport:
        observation: GuardObservation = await asyncio.to_thread(
            read_observation, self._paths, self._last_heartbeat
        )
        self._last_heartbeat = observation.last_heartbeat
        return evaluate(observation, self._clock())

    async def order(
        self,
        order: GuardOrder,
        container: str | None = None,
        reply_seconds: float | None = None,
    ) -> GuardOrderResult:
        async with self._lock:
            seq, instance = await self._write(order, container)
            return await self._reply(order, seq, instance, reply_seconds)

    async def post(self, order: GuardOrder) -> None:
        async with self._lock:
            await self._write(order, None)

    async def _write(self, order: GuardOrder, container: str | None) -> tuple[int, str]:
        if self._last_order_at is not None:
            wait = self._last_order_at + self._timings.gap_seconds - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
        details = active_details(await self.observe())
        instance = details.instance
        if instance is None:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
        seq = details.last_seq + 1
        if self._written is not None and self._written[0] == instance:
            seq = max(seq, self._written[1] + 1)
        if seq > MAX_SEQ:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
        line = order_line(seq, order, container)
        try:
            await asyncio.to_thread(write_order, self._paths.request, line)
        except OSError:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_INACTIVE) from None
        self._last_order_at = self._monotonic()
        self._written = (instance, seq)
        return seq, instance

    async def _reply(
        self, order: GuardOrder, seq: int, instance: str, reply_seconds: float | None
    ) -> GuardOrderResult:
        wait = self._timings.reply_seconds if reply_seconds is None else reply_seconds
        deadline = self._monotonic() + wait
        while True:
            await self._sleep(self._timings.poll_seconds)
            details = active_details(await self.observe())
            if details.instance != instance:
                raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
            last = details.last_order
            if last is not None and last.seq == seq and last.order is order:
                return last.result
            if details.last_seq > seq or self._monotonic() >= deadline:
                raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
