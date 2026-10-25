"""Probelauf über den Wächter auf Klein (Konzept 4.3, „Probelauf in TwinRaid“, ab v0.5.1).

Der Worker gibt dem Wächter nur Aufträge (snapshot, prepare, close) und liest seinen
Status; Share-Daten liest und schreibt TwinRaid nie. Der echte Sync (begin, stop, send …)
folgt mit v0.5.2; bis dahin lehnt der Worker sync_start mit next_version ab.

Ressource ist der Sync: höchstens ein Probelauf zur Zeit, keine Warteschlange. Muss Groß
geweckt werden, belegt der Probelauf dafür auch Groß (über den ActionCoordinator).
"""

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.control import ActionError
from twinraid.integrations.guard_orders import GuardChannel, GuardOrderError, active_details
from twinraid.integrations.source_guard import GuardReport, SourceGuardSource
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome, ActionStep, Trigger
from twinraid.shared.mode import RunMode
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheck,
    GuardCheckResult,
    GuardDetails,
    GuardJob,
    GuardJobResult,
    GuardJobState,
    GuardOrder,
    GuardOrderResult,
    GuardSelfDataset,
    GuardState,
    GuardZfsState,
)
from twinraid.shared.status import ResourceState, ResourceStatus
from twinraid.shared.sync import (
    SyncKind,
    SyncLevels,
    SyncMirror,
    SyncReason,
    SyncReport,
    SyncResult,
    SyncRun,
    SyncShareReport,
    SyncStatus,
    SyncStep,
)
from twinraid.worker.action_log import STARTED, ActionLog, new_action_id

logger = logging.getLogger(__name__)

# Das eigene Dataset von TwinRaid zählt nur, wenn appdata gespiegelt wird (E18).
APPDATA = "appdata"
# Ergebnisse von snapshot, die eine eigene Bedeutung im Aktionslog haben (Konzept 5.3).
_SNAPSHOT_OUTCOMES: dict[GuardOrderResult, ActionOutcome] = {
    GuardOrderResult.TOO_SOON: ActionOutcome.TOO_SOON,
    GuardOrderResult.SNAPSHOT_LOW_SPACE: ActionOutcome.SNAPSHOT_LOW_SPACE,
    GuardOrderResult.SNAPSHOT_LIMIT: ActionOutcome.SNAPSHOT_LIMIT,
}
# Erster Eintrag in blockers mit eigenem Ergebnis; alle übrigen ergeben target_refused.
_BLOCKER_OUTCOMES: dict[GuardBlocker, ActionOutcome] = {
    GuardBlocker.BASE_MISSING: ActionOutcome.BASE_MISSING,
    GuardBlocker.SELF_DATASET_MISSING: ActionOutcome.SELF_DATASET_MISSING,
}
_WAITING_STATES = frozenset({GuardJobState.SNAPSHOT, GuardJobState.PREPARING})
_REPORT_STATES = frozenset({GuardJobState.PREPARED, GuardJobState.AWAITING_CONFIRM})
# Offene Jobs ohne Übertragung, die der Worker nach seinem Start schließt (Konzept 3.6).
_STALE_STATES = frozenset(
    {
        GuardJobState.SNAPSHOT,
        GuardJobState.PREPARING,
        GuardJobState.PREPARED,
        GuardJobState.AWAITING_CONFIRM,
    }
)


@dataclass(frozen=True)
class SyncTimings:
    """Feste Zeiten (Konzept 4.3, „Probelauf in TwinRaid“)."""

    job_poll_seconds: float = 2.0
    # Die Frist im Wächter beträgt 30 min.
    prepare_wait_seconds: float = 31 * 60.0
    # Nach dem Start des Workers: so oft auf einen aktiven Wächter warten.
    startup_poll_seconds: float = 5.0


class TargetActions(Protocol):
    """Was der Probelauf vom ActionCoordinator braucht: Groß belegen und wecken."""

    def wake_enabled(self) -> bool: ...

    def target_busy(self) -> bool: ...

    def running_step(self, run_id: str) -> ActionStep | None: ...

    def reserve_target(self, run_id: str, part_of: ActionName) -> bool: ...

    def release_target(self, run_id: str) -> None: ...

    async def wake_for(self, run_id: str, part_of: ActionName) -> None: ...


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...


class _End(Exception):  # noqa: N818 - Ende eines Probelaufs, kein Fehler im eigentlichen Sinn
    def __init__(self, outcome: ActionOutcome, reason: SyncReason | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.reason = reason


@dataclass
class _Run:
    id: str
    job_id: str
    started_at: datetime
    step: SyncStep
    # Vom Nutzer abgebrochen (sync_cancel); sonst endet der Worker.
    cancel_requested: bool = False
    # snapshot ist geschrieben; ob ein Job entstand, zeigt erst die Antwort.
    snapshot_sent: bool = False
    # Instanz des Wächters und Nummer des Jobs nach snapshot.
    instance: str | None = None
    guard_job: int | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def new_job_id() -> str:
    """Kennung eines Probelaufs für sync_cancel (32 Hex-Zeichen, Konzept 3.2)."""
    return secrets.token_hex(16)


class SyncCoordinator:
    def __init__(
        self,
        channel: GuardChannel,
        guard: SourceGuardSource,
        status: StatusSource,
        config: TwinRaidConfig,
        mode: RunMode,
        action_log: ActionLog,
        timings: SyncTimings | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._channel = channel
        self._guard = guard
        self._status = status
        self._config = config
        self._mode = mode
        self._log = action_log
        self._timings = timings or SyncTimings()
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._target: TargetActions | None = None
        self._run: _Run | None = None
        self._task: asyncio.Task[None] | None = None
        self._last: SyncResult | None = None
        self._report: SyncReport | None = None

    def attach(self, target: TargetActions) -> None:
        self._target = target

    # --- Verfügbarkeit ---

    def dry_run_enabled(self) -> bool:
        if self._mode is RunMode.MOCK:
            return True
        return self._config.actions.sync_dry_run or self._config.actions.sync

    def is_running(self) -> bool:
        """Kopierjob läuft (Konzept 4.1): ein Probelauf oder ein offener Job beim Wächter."""
        if self._run is not None:
            return True
        details = self._guard.report().status.details
        return details is not None and details.job is not None and details.job.open

    def blocker(self, name: ActionName, status: ResourceStatus) -> ActionBlocker | None:
        if name is ActionName.SYNC_START:
            if self._mode is RunMode.REAL and not self._config.actions.sync:
                return ActionBlocker.NOT_ENABLED
            # Der echte Sync kommt mit v0.5.2.
            return ActionBlocker.NEXT_VERSION
        if not self.dry_run_enabled():
            return ActionBlocker.NOT_ENABLED
        if self._run is not None:
            return ActionBlocker.BUSY
        guard = self._guard.report().status
        details = guard.details
        if (
            guard.state is not GuardState.ACTIVE
            or details is None
            or details.zfs.state is not GuardZfsState.OK
        ):
            return ActionBlocker.SOURCE_GUARD_INACTIVE
        shares = set(self._config.sync.shares)
        if shares != {dataset.name for dataset in details.zfs.datasets}:
            return ActionBlocker.LEVELS_MISMATCH
        if APPDATA in shares and details.zfs.self_dataset is not GuardSelfDataset.FOUND:
            return ActionBlocker.SELF_DATASET_MISSING
        return self._target_blocker(status)

    def _target_blocker(self, status: ResourceStatus) -> ActionBlocker | None:
        target = self._target
        if target is None or target.target_busy():
            return ActionBlocker.BUSY
        state = status.target.state
        if state is ResourceState.OFFLINE:
            return None if target.wake_enabled() else ActionBlocker.TARGET_OFFLINE
        if state is not ResourceState.ONLINE:
            return ActionBlocker.TARGET_UNKNOWN
        return None

    # --- Status ---

    def snapshot(self) -> SyncStatus:
        run = self._run
        running = None
        if run is not None:
            step = run.step
            if (
                step is SyncStep.WAKING
                and self._target is not None
                and self._target.running_step(run.id) is ActionStep.WAITING_FOR_VM_SERVICE
            ):
                step = SyncStep.WAITING_FOR_VM_SERVICE
            running = SyncRun(
                kind=SyncKind.DRY_RUN, job_id=run.job_id, step=step, started_at=run.started_at
            )
        return SyncStatus(
            running=running, last=self._last, report=self._report, levels=self._levels()
        )

    def _levels(self) -> SyncLevels:
        details = self._guard.report().status.details
        source = None
        target = None
        checked_at = None
        if details is not None:
            source = tuple(dataset.name for dataset in details.zfs.datasets)
            check = details.target_check
            if check is not None and check.result is GuardCheckResult.OK:
                target = tuple(mirror.name for mirror in check.mirrors)
                checked_at = check.at
        return SyncLevels(
            configured=tuple(self._config.sync.shares),
            source=source,
            target=target,
            target_checked_at=checked_at,
        )

    # --- Befehle ---

    async def request(self, name: ActionName) -> ActionBlocker | None:
        blocker = self.blocker(name, self._status.current())
        if blocker is not None:
            return blocker
        run = _Run(new_action_id(), new_job_id(), self._clock(), SyncStep.SNAPSHOT)
        if self._status.current().target.state is ResourceState.OFFLINE:
            if self._target is None:  # pragma: no cover - der Worker verbindet beide immer
                return ActionBlocker.BUSY
            # Groß sofort belegen, nicht erst, wenn der Ablauf beginnt.
            if not self._target.reserve_target(run.id, ActionName.SYNC_DRY_RUN):
                return ActionBlocker.BUSY
            run.step = SyncStep.WAKING
        self._run = run
        self._task = asyncio.create_task(self._execute(run))
        self._task.add_done_callback(lambda _task: self._abandoned(run))
        return None

    def _abandoned(self, run: _Run) -> None:
        """Ein Abbruch vor dem ersten Schritt erreicht _execute nie: dann hier aufräumen."""
        if self._run is not run:
            return
        if self._target is not None:
            self._target.release_target(run.id)
        outcome = ActionOutcome.CANCELLED if run.cancel_requested else ActionOutcome.INTERRUPTED
        self._finish(run, outcome, None)

    def cancel(self, job_id: str) -> bool:
        """Bricht den laufenden Probelauf ab; False, wenn keiner mit dieser Kennung läuft."""
        run = self._run
        if run is None or run.job_id != job_id or self._task is None:
            return False
        run.cancel_requested = True
        self._task.cancel()
        return True

    async def close(self) -> None:
        """Der Worker endet: ein laufender Probelauf steht als interrupted im Aktionslog."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _execute(self, run: _Run) -> None:
        self._log.record(run.id, ActionName.SYNC_DRY_RUN, Trigger.DASHBOARD, STARTED)
        outcome, reason = ActionOutcome.DRY_RUN_SUCCESS, None
        try:
            await self._dry_run(run)
        except _End as end:
            outcome, reason = end.outcome, end.reason
        except GuardOrderError as failure:
            outcome = failure.outcome
            await self._close_quietly(run)
        except ActionError as failure:
            outcome = failure.outcome
        except asyncio.CancelledError:
            if not run.cancel_requested:
                self._finish(run, ActionOutcome.INTERRUPTED, None)
                raise
            outcome = ActionOutcome.CANCELLED
            await self._close_quietly(run)
        except Exception:  # Ein Probelauf darf den Worker nie beenden.
            logger.exception("dry run failed unexpectedly")
            outcome = ActionOutcome.INTERNAL_ERROR
            await self._close_quietly(run)
        finally:
            if self._target is not None:
                self._target.release_target(run.id)
        self._finish(run, outcome, reason)

    def _finish(self, run: _Run, outcome: ActionOutcome, reason: SyncReason | None) -> None:
        self._log.record(run.id, ActionName.SYNC_DRY_RUN, Trigger.DASHBOARD, outcome, reason=reason)
        self._last = SyncResult(
            kind=SyncKind.DRY_RUN, outcome=outcome, reason=reason, finished_at=self._clock()
        )
        if self._run is run:
            self._run = None

    async def _dry_run(self, run: _Run) -> None:
        if run.step is SyncStep.WAKING:
            if self._target is None:  # pragma: no cover - der Worker verbindet beide immer
                raise _End(ActionOutcome.INTERNAL_ERROR)
            await self._target.wake_for(run.id, ActionName.SYNC_DRY_RUN)
        run.step = SyncStep.SNAPSHOT
        run.snapshot_sent = True
        result = await self._channel.order(GuardOrder.SNAPSHOT)
        if result is not GuardOrderResult.CREATED:
            run.snapshot_sent = False
            if result in _SNAPSHOT_OUTCOMES:
                raise _End(_SNAPSHOT_OUTCOMES[result])
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)
        details = active_details(await self._channel.observe())
        job = details.job
        if job is None or job.state is not GuardJobState.SNAPSHOT:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
        run.instance, run.guard_job = details.instance, job.id
        run.step = SyncStep.PREPARING
        result = await self._channel.order(GuardOrder.PREPARE)
        if result is not GuardOrderResult.PREPARING:
            await self._close_quietly(run)
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)
        job, details = await self._wait_for_prepare(run)
        if job is None:
            raise _End(ActionOutcome.JOB_TIMEOUT)
        self._report = _report(job, details.target_check, self._clock())
        outcome, reason = evaluate_prepare(job, details.target_check)
        run.step = SyncStep.CLOSING
        await self._close_quietly(run)
        raise _End(outcome, reason)

    async def _wait_for_prepare(self, run: _Run) -> tuple[GuardJob | None, GuardDetails]:
        """Wartet, bis prepare fertig ist; None nach der eigenen Frist (Wächter hing)."""
        deadline = self._monotonic() + self._timings.prepare_wait_seconds
        while True:
            details = active_details(await self._channel.observe())
            job = details.job
            if details.instance != run.instance or job is None or job.id != run.guard_job:
                raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
            if job.state in _REPORT_STATES:
                return job, details
            if job.state not in _WAITING_STATES:
                raise _End(_ended_job_outcome(job))
            if self._monotonic() >= deadline:
                await self._close_quietly(run)
                return None, details
            await self._sleep(self._timings.job_poll_seconds)

    async def _close_quietly(self, run: _Run) -> None:
        """close für den eigenen Job; scheitert es, schließt der Wächter ihn nach seiner Frist.

        Kam die Antwort auf snapshot nicht mehr an (Abbruch, Frist), zeigt der Status, ob ein
        Job nach dem Beginn dieses Probelaufs entstand; nur TwinRaid schreibt Aufträge.
        """
        if run.guard_job is None and not (run.snapshot_sent and await self._own_job_open(run)):
            return
        try:
            result = await asyncio.shield(self._channel.order(GuardOrder.CLOSE))
        except GuardOrderError, asyncio.CancelledError:
            logger.warning("dry run: close was not confirmed by the source guard")
            return
        if result not in (GuardOrderResult.CLOSED, GuardOrderResult.NO_JOB):
            logger.warning("dry run: close answered %s", result.value)
        run.guard_job = None
        run.snapshot_sent = False

    async def _own_job_open(self, run: _Run) -> bool:
        try:
            details = active_details(await asyncio.shield(self._channel.observe()))
        except GuardOrderError, asyncio.CancelledError:
            return False
        job = details.job
        return (
            job is not None
            and job.state in _STALE_STATES
            and job.created_at >= _seconds(run.started_at)
        )

    # --- Nach dem Start des Workers ---

    async def run(self) -> None:
        """Schließt einmal einen offenen Job, den kein Probelauf dieses Workers führt (3.6)."""
        while True:
            try:
                report = await self._channel.observe()
            except Exception:  # Lesefehler: später erneut.
                logger.exception("reading the source guard for stale jobs failed")
                report = None
            if report is not None and report.status.state is GuardState.ACTIVE:
                await self._close_stale(report)
                return
            await self._sleep(self._timings.startup_poll_seconds)

    async def _close_stale(self, report: GuardReport) -> None:
        details = report.status.details
        job = details.job if details is not None else None
        if job is None or job.state not in _STALE_STATES or self._run is not None:
            return
        try:
            result = await self._channel.order(GuardOrder.CLOSE)
        except GuardOrderError as failure:
            logger.warning("closing an open job of the source guard failed: %s", failure)
            return
        logger.info("closed an open job of the source guard after the start: %s", result.value)


def _seconds(moment: datetime) -> datetime:
    """Zeitpunkte des Wächters haben ganze Sekunden."""
    return moment.replace(microsecond=0)


def _ended_job_outcome(job: GuardJob) -> ActionOutcome:
    """Job während prepare beendet: Fehler auf Klein, Frist oder Neustart des Wächters."""
    match job.result:
        case GuardJobResult.PREPARE_FAILED:
            return ActionOutcome.PREPARE_FAILED
        case GuardJobResult.JOB_TIMEOUT:
            return ActionOutcome.JOB_TIMEOUT
        case _:
            return ActionOutcome.SOURCE_GUARD_LOST


def evaluate_prepare(
    job: GuardJob, check: GuardCheck | None
) -> tuple[ActionOutcome, SyncReason | None]:
    """Ergebnis eines fertigen prepare (Konzept 4.3, „Probelauf in TwinRaid“, „Ergebnis“)."""
    if check is None:
        return ActionOutcome.SOURCE_GUARD_LOST, None
    if check.result is not GuardCheckResult.OK:
        return ActionOutcome.TARGET_CHECK_FAILED, check.result
    if job.send_ready is True:
        return ActionOutcome.DRY_RUN_SUCCESS, None
    if not job.blockers:
        return ActionOutcome.TARGET_REFUSED, None
    first = job.blockers[0]
    if first is GuardBlocker.TARGET_UNREACHABLE:
        return ActionOutcome.TARGET_CHECK_FAILED, check.result
    if first in _BLOCKER_OUTCOMES:
        return _BLOCKER_OUTCOMES[first], None
    return ActionOutcome.TARGET_REFUSED, first


def _report(job: GuardJob, check: GuardCheck | None, now: datetime) -> SyncReport:
    mirrors = {}
    if check is not None:
        mirrors = {
            mirror.name: SyncMirror(
                state=mirror.state,
                props=mirror.props,
                count=mirror.count,
                newest_received=mirror.newest_received,
            )
            for mirror in check.mirrors
        }
    return SyncReport(
        prepared_at=now,
        snapshot=job.snapshot,
        awaiting_confirm=job.state is GuardJobState.AWAITING_CONFIRM,
        confirm_reasons=job.confirm_reasons,
        send_ready=job.send_ready is True,
        blockers=job.blockers,
        check=check,
        shares=tuple(
            SyncShareReport(
                name=item.name,
                mode=item.mode,
                base_type=item.base_type,
                problem=item.problem,
                deleted=item.deleted,
                modified=item.modified,
                added=item.added,
                renamed=item.renamed,
                referenced=item.referenced,
                base_referenced=item.base_referenced,
                size=item.size,
                mirror=mirrors.get(item.name),
            )
            for item in job.datasets
        ),
    )
