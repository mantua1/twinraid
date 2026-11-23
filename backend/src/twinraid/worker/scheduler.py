"""Zeitplan im Worker (Konzept 4.8, Entscheidung E29, ab v0.6.0).

Der Worker prüft den Zeitplan im Takt der Statusprüfung (10 s) mit der Uhr des Containers.
Ein fälliger Termin startet denselben Ablauf wie „Jetzt synchronisieren“ mit dem Auslöser
schedule. Sperrt etwas, versucht er es alle 5 Minuten erneut, höchstens 60 Minuten nach dem
ersten Versuch; not_enabled und target_offline lassen den Termin sofort aus. Die Prüfung hat
keine Wirkung nach außen: Erst wenn nichts mehr sperrt, hält der Ablauf Container an oder
weckt Groß.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.source_guard import SourceGuardSource
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome, Trigger
from twinraid.shared.mode import RunMode
from twinraid.shared.schedule import (
    ScheduleConflict,
    ScheduleLast,
    ScheduleLimit,
    ScheduleStatus,
    ScheduleWaiting,
)
from twinraid.shared.source_guard import GuardCheckResult, GuardState
from twinraid.shared.status import ResourceState, ResourceStatus
from twinraid.shared.sync import SyncHistoryEntry, SyncKind
from twinraid.sync.schedule import (
    ScheduleState,
    ScheduleStateError,
    due_term,
    format_term,
    next_terms,
    shortest_gap,
)
from twinraid.worker.action_log import ActionLog, new_action_id
from twinraid.worker.sync_history import SyncHistory

logger = logging.getLogger(__name__)

# Sperren, nach denen ein Termin sofort ausgelassen wird (Konzept 4.8).
_SKIP_AT_ONCE = frozenset({ActionBlocker.NOT_ENABLED, ActionBlocker.TARGET_OFFLINE})


@dataclass(frozen=True)
class ScheduleTimings:
    tick_seconds: float = 10.0
    # Wächter und Groß sind dann schon einmal gelesen.
    startup_delay: timedelta = timedelta(minutes=2)
    retry: timedelta = timedelta(minutes=5)
    retry_window: timedelta = timedelta(minutes=60)


class ScheduledSync(Protocol):
    async def request_scheduled(self, term: datetime) -> ActionBlocker | None: ...


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...


@dataclass
class _Waiting:
    term: datetime
    first_attempt: datetime
    next_attempt: datetime
    reason: ActionBlocker


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    def __init__(
        self,
        config: TwinRaidConfig,
        mode: RunMode,
        sync: ScheduledSync,
        availability: Callable[[], ActionBlocker | None],
        status: StatusSource,
        guard: SourceGuardSource,
        state: ScheduleState,
        action_log: ActionLog,
        history: SyncHistory,
        timings: ScheduleTimings | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._schedule = config.sync.schedule
        self._mode = mode
        self._sync = sync
        self._availability = availability
        self._status = status
        self._guard = guard
        self._state = state
        self._log = action_log
        self._history = history
        self._timings = timings or ScheduleTimings()
        self._clock = clock
        self._sleep = sleep
        self._started_at = clock()
        self._last_handled = self._load_last_handled()
        self._waiting: _Waiting | None = None
        self._gap: timedelta | None = None
        self._conflicts: tuple[ScheduleConflict, ...] = ()
        self._conflict_logged = False

    # --- Freischaltung ---

    @property
    def enabled(self) -> bool:
        return self._schedule.enabled

    def permitted(self) -> bool:
        """Im Modus real braucht der Zeitplan actions.sync (Konzept 4.2)."""
        return self._mode is RunMode.MOCK or self._config.actions.sync

    def _wakes(self) -> bool:
        wake = self._mode is RunMode.MOCK or self._config.actions.wake
        return self._schedule.wake_target and wake

    def _shuts_down(self) -> bool:
        shutdown = self._mode is RunMode.MOCK or self._config.actions.shutdown
        return self._schedule.shutdown_after and shutdown

    def _play_shuts_down(self) -> bool:
        shutdown = self._mode is RunMode.MOCK or self._config.actions.shutdown
        return self._config.gaming_vm.shutdown_target_after.enabled and shutdown

    # --- Zuletzt behandelter Termin ---

    def _load_last_handled(self) -> datetime:
        """Fehlt schedule.json oder ist sie nicht lesbar, gilt kein früherer Termin als
        verpasst: Es zählen nur Termine nach dem Start des Workers (Konzept 4.8)."""
        try:
            stored = self._state.load()
        except ScheduleStateError as exc:
            if self._schedule.enabled:
                logger.error(
                    "schedule.json is not readable (%s); no earlier term counts as missed", exc
                )
            return self._started_at
        return stored if stored is not None else self._started_at

    def _handled(self, term: datetime) -> None:
        """Ein Termin gilt als behandelt, sobald der Worker ihn angeht."""
        self._last_handled = term
        try:
            self._state.save(term)
        except OSError:
            logger.exception("could not write schedule.json; the term is kept in memory only")

    # --- Ablauf ---

    def log_start(self) -> None:
        """Startlog: nächster Termin, ob geweckt und heruntergefahren wird (Konzept 4.8)."""
        if not self.enabled:
            logger.info("Schedule: off (sync.schedule.enabled is false).")
            return
        terms = next_terms(self._schedule, self._clock(), 1)
        logger.info(
            "Next scheduled run: %s; wakes the target: %s; shuts it down afterwards: %s",
            format_term(terms[0], self._schedule.timezone),
            "yes" if self._wakes() else "no",
            "yes" if self._shuts_down() else "no",
        )
        if not self.permitted():
            logger.warning("Schedule: not enabled in actions (actions.sync); it is not run.")

    async def run(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                await self.tick()
            except Exception:  # Der Zeitplan darf den Worker nie beenden.
                logger.exception("schedule check failed")
            await self._sleep(self._timings.tick_seconds)

    async def tick(self) -> None:
        now = self._clock()
        self._check_interval()
        if now < self._started_at + self._timings.startup_delay:
            return
        if self._waiting is not None:
            if now >= self._waiting.next_attempt:
                await self._attempt(self._waiting, now)
            return
        due = due_term(self._schedule, self._last_handled, now)
        if due is None:
            return
        self._handled(due.term)
        if due.missed:
            logger.warning(
                "schedule_missed: the term %s is more than 6 hours ago",
                format_term(due.term, self._schedule.timezone),
            )
            self._record(due.term, ActionOutcome.SCHEDULE_MISSED, None, now)
            return
        waiting = _Waiting(due.term, now, now, ActionBlocker.BUSY)
        await self._attempt(waiting, now)

    async def _attempt(self, waiting: _Waiting, now: datetime) -> None:
        blocker = self._blocker()
        if blocker is None:
            blocker = await self._sync.request_scheduled(waiting.term)
            if blocker is None:
                logger.info(
                    "scheduled run for %s started",
                    format_term(waiting.term, self._schedule.timezone),
                )
                self._waiting = None
                return
        until = waiting.first_attempt + self._timings.retry_window
        if blocker in _SKIP_AT_ONCE or now >= until:
            self._waiting = None
            self._record(waiting.term, ActionOutcome.SCHEDULE_SKIPPED, blocker, now)
            return
        waiting.reason = blocker
        waiting.next_attempt = min(now + self._timings.retry, until)
        if self._waiting is None:
            logger.info("scheduled run waits: %s", blocker.value)
        self._waiting = waiting

    def _blocker(self) -> ActionBlocker | None:
        if not self.permitted():
            return ActionBlocker.NOT_ENABLED
        blocker = self._availability()
        offline = self._status.current().target.state is ResourceState.OFFLINE
        if blocker is None and offline and not self._wakes():
            # Wecken nur mit wake_target (Konzept 4.8); sonst wird der Termin ausgelassen.
            return ActionBlocker.TARGET_OFFLINE
        return blocker

    def _record(
        self, term: datetime, outcome: ActionOutcome, reason: ActionBlocker | None, now: datetime
    ) -> None:
        """Ausgelassener oder verpasster Termin: Aktionslog und eine Zeile ohne Shares."""
        action_id = new_action_id()
        self._log.record(action_id, ActionName.SYNC_START, Trigger.SCHEDULE, outcome, reason=reason)
        self._history.append(
            SyncHistoryEntry(
                id=action_id,
                kind=SyncKind.SYNC,
                trigger=Trigger.SCHEDULE,
                started_at=now,
                finished_at=now,
                result=outcome,
                reason=reason,
                woke_target=False,
                containers_stopped=0,
                adopted=False,
                shares=(),
                scheduled_for=term,
            )
        )

    # --- Takt gegen die Grenzen des Wächters ---

    def _check_interval(self) -> None:
        """Ist eine Grenze von Wächter oder Groß so lang wie der kürzeste Abstand zwischen zwei
        Terminen oder länger? Einmal eine Warnung, dauerhaft ein Hinweis (Konzept 4.8)."""
        report = self._guard.report().status
        details = report.details
        if report.state is not GuardState.ACTIVE or details is None:
            return
        if self._gap is None:
            self._gap = shortest_gap(self._schedule, self._clock())
        limits: list[tuple[ScheduleLimit, int]] = [
            (ScheduleLimit.MIN_SNAPSHOT_INTERVAL, details.limits.min_snapshot_interval_minutes)
        ]
        if self._config.sync.stop_containers:
            limits.insert(
                0, (ScheduleLimit.MIN_RUN_INTERVAL, details.limits.min_run_interval_minutes)
            )
        check = details.target_check
        summary = check.summary if check is not None else None
        if (
            check is not None
            and check.result is GuardCheckResult.OK
            and summary is not None
            and summary.min_interval_minutes is not None
        ):
            limits.append((ScheduleLimit.MIN_RECEIVE_INTERVAL, summary.min_interval_minutes))
        gap = self._gap
        self._conflicts = tuple(
            ScheduleConflict(limit=limit, minutes=minutes)
            for limit, minutes in limits
            if timedelta(minutes=minutes) >= gap
        )
        if self._conflicts and not self._conflict_logged:
            self._conflict_logged = True
            names = ", ".join(f"{c.limit.value}={c.minutes}" for c in self._conflicts)
            logger.warning(
                "schedule_interval_conflict: the shortest gap between two terms is %d min; "
                "these limits are as long or longer: %s",
                gap // timedelta(minutes=1),
                names,
            )

    # --- Status ---

    def snapshot(self) -> ScheduleStatus:
        schedule = self._schedule
        enabled = schedule.enabled
        waiting = self._waiting
        return ScheduleStatus(
            enabled=enabled,
            permitted=self.permitted(),
            timezone=schedule.timezone,
            days=tuple(schedule.days),
            time=schedule.time,
            wake=self._wakes(),
            shutdown=self._shuts_down(),
            shutdown_delay_minutes=schedule.shutdown_delay_minutes,
            play_shutdown=self._play_shuts_down(),
            play_delay_minutes=self._config.gaming_vm.shutdown_target_after.delay_minutes,
            next_terms=tuple(next_terms(schedule, self._clock(), 3)) if enabled else (),
            waiting=(
                ScheduleWaiting(
                    term=waiting.term,
                    reason=waiting.reason,
                    next_attempt_at=waiting.next_attempt,
                    until=waiting.first_attempt + self._timings.retry_window,
                )
                if waiting is not None
                else None
            ),
            last=self._last_scheduled(),
            shortest_gap_minutes=(
                self._gap // timedelta(minutes=1) if self._gap is not None else None
            ),
            conflicts=self._conflicts,
        )

    def _last_scheduled(self) -> ScheduleLast | None:
        for entry in self._history.recent():
            if entry.trigger is Trigger.SCHEDULE and entry.scheduled_for is not None:
                return ScheduleLast(
                    scheduled_for=entry.scheduled_for,
                    outcome=entry.result,
                    reason=entry.reason,
                    finished_at=entry.finished_at,
                )
        return None
