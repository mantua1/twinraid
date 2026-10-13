"""Live-Updates per SSE (Konzept 4.1 und 5.3).

Ein einziger Abfrage-Task fragt den Worker mit get_status und verteilt
Änderungen an alle offenen Datenströme. Dafür gibt es keinen neuen
Worker-Befehl.

Sitzungsregeln: Ein offener Datenstrom zählt nicht als Aktivität. Bei jeder
Statusänderung und mindestens alle 15 Sekunden prüft er, ob seine Session
noch gilt, und endet sonst. Solange ein
Kopierjob läuft, setzt er die letzte Aktivität seiner Session; am Ende des Jobs
einmal auf diesen Zeitpunkt. Die maximale Lebensdauer gilt immer. Das Ende
einer Session beendet nie den Kopierjob: Der Web-Prozess sendet dabei keinen
Befehl an den Worker.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from twinraid.shared.status import StatusSnapshot
from twinraid.web.sessions import Lookup
from twinraid.web.worker_client import WorkerApi, WorkerCommandError, WorkerUnavailableError

logger = logging.getLogger(__name__)

MAX_STREAMS = 32
# Wartezeit des Browsers vor dem Neuverbinden.
RETRY_MILLISECONDS = 5000


@dataclass(frozen=True)
class EventTimings:
    poll_seconds: float = 2.0
    session_check_seconds: float = 15.0
    keepalive_seconds: float = 15.0
    # Danach endet der Datenstrom und der Browser verbindet sich neu (ohne die
    # Session zu verlängern). Begrenzt die Lebensdauer einzelner Verbindungen.
    max_stream_seconds: float = 60.0 * 60.0


@dataclass(frozen=True)
class HubEvent:
    """Status vom Worker oder None, wenn der Worker nicht erreichbar ist."""

    snapshot: StatusSnapshot | None


class StatusHub:
    def __init__(self, worker: WorkerApi, poll_seconds: float) -> None:
        self._worker = worker
        self._poll_seconds = poll_seconds
        self._subscribers: set[asyncio.Queue[HubEvent]] = set()
        self._task: asyncio.Task[None] | None = None
        self._latest: HubEvent | None = None

    @property
    def stream_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[HubEvent]:
        # maxsize=1: Ein langsamer Datenstrom bekommt immer nur den neuesten Stand.
        queue: asyncio.Queue[HubEvent] = asyncio.Queue(maxsize=1)
        if self._latest is not None:
            queue.put_nowait(self._latest)
        self._subscribers.add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_forever())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[HubEvent]) -> None:
        self._subscribers.discard(queue)
        if not self._subscribers and self._task is not None:
            self._task.cancel()
            self._task = None
            self._latest = None

    async def _poll_forever(self) -> None:
        while True:
            try:
                event = HubEvent(await self._worker.get_status())
            except WorkerUnavailableError, WorkerCommandError:
                event = HubEvent(None)
            if event != self._latest:
                self._latest = event
                for queue in self._subscribers:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(event)
            await asyncio.sleep(self._poll_seconds)


def _format(event: str, data: object) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def format_status(event: HubEvent) -> bytes:
    if event.snapshot is None:
        return _format("worker_unavailable", {})
    return _format("status", event.snapshot.model_dump(mode="json"))


SessionCheck = Callable[[bool, bool], Awaitable[Lookup]]


async def _still_valid(
    check_session: SessionCheck,
    touch: bool,
    force: bool,
    on_expired: Callable[[], Awaitable[None]],
) -> bool:
    lookup = await check_session(touch, force)
    if lookup.session is not None:
        return True
    if lookup.expired:
        await on_expired()
    return False


async def event_stream(
    hub: StatusHub,
    check_session: SessionCheck,
    timings: EventTimings,
    on_expired: Callable[[], Awaitable[None]],
) -> AsyncIterator[bytes]:
    """Datenstrom einer Session. check_session(touch, force) prüft und verlängert sie."""
    loop = asyncio.get_running_loop()
    queue = hub.subscribe()
    try:
        yield f"retry: {RETRY_MILLISECONDS}\n\n".encode()
        started = loop.time()
        next_check = started + timings.session_check_seconds
        next_keepalive = started + timings.keepalive_seconds
        job_running = False
        while True:
            now = loop.time()
            if now - started >= timings.max_stream_seconds:
                return
            deadline = min(next_check, next_keepalive, started + timings.max_stream_seconds)
            wait = max(0.0, deadline - now)
            event: HubEvent | None = None
            with contextlib.suppress(TimeoutError):
                event = await asyncio.wait_for(queue.get(), timeout=wait)
            now = loop.time()
            if event is not None or now >= next_check:
                running = job_running
                if event is not None:
                    running = event.snapshot is not None and event.snapshot.job_running
                # Während eines Kopierjobs hält ein offenes Dashboard seine Session aktiv;
                # an seinem Ende beginnt die Inaktivität sofort neu.
                ended = job_running and not running
                if not await _still_valid(check_session, running or ended, ended, on_expired):
                    yield _format("session_ended", {})
                    return
                job_running = running
                next_check = now + timings.session_check_seconds
                if event is not None:
                    yield format_status(event)
            if now >= next_keepalive:
                yield b": keepalive\n\n"
                next_keepalive = now + timings.keepalive_seconds
    finally:
        hub.unsubscribe(queue)
