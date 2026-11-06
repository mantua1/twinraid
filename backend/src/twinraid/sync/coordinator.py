"""Probelauf und Sync über den Wächter auf Klein (Konzept 4.3, ab v0.5.1 bzw. v0.5.2).

Der Worker gibt dem Wächter nur Aufträge (begin, stop, ping, snapshot, finish, prepare,
confirm, send, close) und liest seinen Status; Share-Daten liest und schreibt TwinRaid nie.

Ressource ist der Sync: höchstens ein Lauf zur Zeit (Probelauf oder Sync), keine
Warteschlange. Muss Groß geweckt werden, belegt der Lauf dafür auch Groß (über den
ActionCoordinator).
"""

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from twinraid.config import TwinRaidConfig
from twinraid.integrations.control import ActionError
from twinraid.integrations.guard_orders import GuardChannel, GuardOrderError, active_details
from twinraid.integrations.source_guard import GuardReport, SourceGuardSource
from twinraid.shared.actions import (
    ActionBlocker,
    ActionName,
    ActionOutcome,
    ActionStep,
    SyncPart,
    Trigger,
)
from twinraid.shared.mode import RunMode
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheck,
    GuardCheckResult,
    GuardContainerState,
    GuardDetails,
    GuardJob,
    GuardJobReason,
    GuardJobResult,
    GuardJobState,
    GuardOrder,
    GuardOrderResult,
    GuardRestart,
    GuardSelfDataset,
    GuardState,
    GuardZfsState,
)
from twinraid.shared.status import ResourceState, ResourceStatus
from twinraid.shared.sync import (
    SyncConfirmation,
    SyncHistoryEntry,
    SyncHistoryShare,
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
from twinraid.sync.progress import ProgressTracker
from twinraid.worker.action_log import STARTED, ActionLog, new_action_id
from twinraid.worker.sync_history import SyncHistory

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
# Gründe eines gescheiterten Sendens mit eigenem Ergebnis (Konzept 4.3, „Ergebnis“).
_SEND_REASON_OUTCOMES: dict[GuardJobReason, ActionOutcome] = {
    GuardJobReason.BASE_MISSING: ActionOutcome.BASE_MISSING,
    GuardJobReason.SELF_DATASET_MISSING: ActionOutcome.SELF_DATASET_MISSING,
}
# Ergebnisse von finish je Container, nach denen ein Container nicht wieder läuft.
_RESTART_FAILED = frozenset({GuardRestart.START_FAILED, GuardRestart.CHANGED, GuardRestart.GONE})
# Aufträge, die den Lauf ändern: Ein Abbruch wartet ihre Antwort ab (siehe _order).
_RUN_ORDERS = frozenset({GuardOrder.BEGIN, GuardOrder.STOP, GuardOrder.FINISH})
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
    """Feste Zeiten (Konzept 4.3)."""

    job_poll_seconds: float = 2.0
    # Die Fristen im Wächter betragen 30 min für prepare und die Freigabe, 12 h für das Senden.
    prepare_wait_seconds: float = 31 * 60.0
    confirm_wait_seconds: float = 31 * 60.0
    send_wait_seconds: float = 12 * 3600.0 + 60.0
    # Solange ein Lauf offen ist, ping in diesem Abstand.
    ping_seconds: float = 15.0
    # Antwort auf stop: stop_timeout_seconds des Wächters plus diese Zeit (Konzept 3.6).
    stop_reply_extra_seconds: float = 15.0
    # Nach dem Start des Workers: so oft auf einen aktiven Wächter warten.
    startup_poll_seconds: float = 5.0


class TargetActions(Protocol):
    """Was ein Lauf vom ActionCoordinator braucht: Groß belegen und wecken."""

    def wake_enabled(self) -> bool: ...

    def target_busy(self) -> bool: ...

    def running_step(self, run_id: str) -> ActionStep | None: ...

    def reserve_target(self, run_id: str, part_of: ActionName) -> bool: ...

    def release_target(self, run_id: str) -> None: ...

    async def wake_for(self, run_id: str, part_of: ActionName) -> None: ...


class StatusSource(Protocol):
    def current(self) -> ResourceStatus: ...


class _End(Exception):  # noqa: N818 - Ende eines Laufs, kein Fehler im eigentlichen Sinn
    def __init__(self, outcome: ActionOutcome, reason: SyncReason | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.reason = reason


@dataclass
class _Run:
    id: str
    job_id: str
    kind: SyncKind
    started_at: datetime
    step: SyncStep
    # Vom Nutzer abgebrochen (sync_cancel); sonst endet der Worker.
    cancel_requested: bool = False
    # snapshot ist geschrieben; ob ein Job entstand, zeigt erst die Antwort.
    snapshot_sent: bool = False
    # Instanz des Wächters und Nummer des eigenen Jobs nach snapshot; None, wenn er endete.
    instance: str | None = None
    guard_job: int | None = None
    # Zuletzt gesehener Stand des Jobs (Bericht und Historie).
    job: GuardJob | None = None
    # Container-Phase (nur Sync): Lauf offen, angehalten, Totmannschalter, Neustart gescheitert.
    run_open: bool = False
    containers_stopped: int = 0
    deadman: bool = False
    restart_failed: bool = False
    # Der Worker endet und hat finish oder close nur noch geschrieben.
    posted: bool = False
    woke: bool = False
    adopted: bool = False
    # send ist geschrieben: Beim Ende des Workers läuft die Übertragung weiter.
    sending: bool = False
    confirmation: SyncConfirmation | None = None
    confirmed: asyncio.Event = field(default_factory=asyncio.Event)
    progress: ProgressTracker | None = None
    # Der Ablauf hat begonnen; vorher wirkt ein Abbruch erst zu Beginn des Ablaufs.
    started: bool = False

    @property
    def action(self) -> ActionName:
        return ActionName.SYNC_DRY_RUN if self.kind is SyncKind.DRY_RUN else ActionName.SYNC_START


def _utc_now() -> datetime:
    return datetime.now(UTC)


def new_job_id() -> str:
    """Kennung eines Laufs für sync_cancel und sync_confirm_deletions (Konzept 3.2)."""
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
        history: SyncHistory | None = None,
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
        self._history = history or SyncHistory(None)
        self._timings = timings or SyncTimings()
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._target: TargetActions | None = None
        self._run: _Run | None = None
        self._task: asyncio.Task[None] | None = None
        self._last: SyncResult | None = None
        self._report: SyncReport | None = None
        # Der Worker endet: Aufträge nur noch schreiben, nicht mehr auf Antworten warten.
        self._closing = False

    def attach(self, target: TargetActions) -> None:
        self._target = target

    # --- Verfügbarkeit ---

    def dry_run_enabled(self) -> bool:
        if self._mode is RunMode.MOCK:
            return True
        return self._config.actions.sync_dry_run or self._config.actions.sync

    def sync_enabled(self) -> bool:
        return self._mode is RunMode.MOCK or self._config.actions.sync

    def is_running(self) -> bool:
        """Kopierjob läuft (Konzept 4.1): ein Lauf oder ein offener Job beim Wächter."""
        if self._run is not None:
            return True
        details = self._guard.report().status.details
        return details is not None and details.job is not None and details.job.open

    def blocker(self, name: ActionName, status: ResourceStatus) -> ActionBlocker | None:
        """Sperren vor dem Start (Konzept 4.2 und 4.3), in der Reihenfolge des Konzepts."""
        sync = name is ActionName.SYNC_START
        if not (self.sync_enabled() if sync else self.dry_run_enabled()):
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
        if sync and self._too_soon(details):
            return ActionBlocker.SOURCE_GUARD_TOO_SOON
        shares = set(self._config.sync.shares)
        if shares != {dataset.name for dataset in details.zfs.datasets}:
            return ActionBlocker.LEVELS_MISMATCH
        # Den echten Sync sperren auch Befunde aus dem letzten check; den Probelauf nicht,
        # denn nur er frischt check auf (Konzept 4.3, „Probelauf in TwinRaid“).
        check = details.target_check
        checked = check is not None and check.result is GuardCheckResult.OK
        mirrors = {mirror.name for mirror in check.mirrors} if check is not None else set()
        if sync and checked and shares != mirrors:
            return ActionBlocker.LEVELS_MISMATCH
        if APPDATA in shares and details.zfs.self_dataset is not GuardSelfDataset.FOUND:
            return ActionBlocker.SELF_DATASET_MISSING
        clones = check.summary.clones if check is not None and check.summary is not None else None
        if sync and checked and clones:
            return ActionBlocker.TARGET_CLONE_ACTIVE
        return self._target_blocker(status)

    def _too_soon(self, details: GuardDetails) -> bool:
        """Pause des Wächters zwischen zwei Läufen; nur mit stop_containers (Konzept 4.2)."""
        allowed_at = details.run_allowed_at
        return (
            self._config.sync.stop_containers
            and allowed_at is not None
            and allowed_at > self._clock()
        )

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
                kind=run.kind,
                job_id=run.job_id,
                step=step,
                started_at=run.started_at,
                adopted=run.adopted,
                containers_stopped=run.containers_stopped,
                confirmation=run.confirmation,
                progress=run.progress.snapshot() if run.progress is not None else None,
            )
        return SyncStatus(
            running=running,
            last=self._last,
            report=self._report,
            levels=self._levels(),
            history=self._history.recent(),
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
        kind = SyncKind.SYNC if name is ActionName.SYNC_START else SyncKind.DRY_RUN
        run = _Run(new_action_id(), new_job_id(), kind, self._clock(), SyncStep.SNAPSHOT)
        if self._status.current().target.state is ResourceState.OFFLINE:
            if self._target is None:  # pragma: no cover - der Worker verbindet beide immer
                return ActionBlocker.BUSY
            # Groß sofort belegen, nicht erst, wenn der Ablauf beginnt.
            if not self._target.reserve_target(run.id, name):
                return ActionBlocker.BUSY
            run.step = SyncStep.WAKING
        self._start(run)
        return None

    def _start(self, run: _Run) -> None:
        self._run = run
        self._task = asyncio.create_task(self._execute(run))
        self._task.add_done_callback(lambda _task: self._abandoned(run))

    def _abandoned(self, run: _Run) -> None:
        """Ein Abbruch vor dem ersten Schritt erreicht _execute nie: dann hier aufräumen."""
        if self._run is not run:
            return
        if self._target is not None:
            self._target.release_target(run.id)
        outcome = ActionOutcome.CANCELLED if run.cancel_requested else ActionOutcome.INTERRUPTED
        self._finish(run, outcome, None)

    def cancel(self, job_id: str) -> bool:
        """Bricht den laufenden Lauf ab; False, wenn keiner mit dieser Kennung läuft."""
        run = self._run
        if run is None or run.job_id != job_id or self._task is None:
            return False
        if not run.cancel_requested:
            run.cancel_requested = True
            if run.started:
                self._task.cancel()
        return True

    def confirm(self, job_id: str) -> bool:
        """Gibt die Löschungen frei; False, wenn kein Sync mit dieser Kennung darauf wartet."""
        run = self._run
        if (
            run is None
            or run.job_id != job_id
            or run.step is not SyncStep.AWAITING_CONFIRM
            or run.cancel_requested
        ):
            return False
        run.confirmed.set()
        return True

    async def close(self) -> None:
        """Der Worker endet: Ein laufender Lauf steht als interrupted im Aktionslog."""
        self._closing = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _execute(self, run: _Run) -> None:
        run.started = True
        if not run.adopted:
            self._log.record(run.id, run.action, Trigger.DASHBOARD, STARTED)
        outcome, reason = ActionOutcome.INTERNAL_ERROR, None
        try:
            if run.cancel_requested:
                # Abgebrochen, bevor der Ablauf begann (eine übernommene Übertragung braucht
                # trotzdem close).
                raise asyncio.CancelledError
            if run.adopted:
                await self._adopted(run)
            elif run.kind is SyncKind.DRY_RUN:
                await self._dry_run(run)
            else:
                await self._sync(run)
        except _End as end:
            outcome, reason = end.outcome, end.reason
        except GuardOrderError as failure:
            outcome = failure.outcome
            await self._cleanup(run)
        except ActionError as failure:
            outcome = failure.outcome
        except asyncio.CancelledError:
            if not run.cancel_requested:
                await self._leave(run)
                self._finish(run, ActionOutcome.INTERRUPTED, None)
                raise
            declined = run.step is SyncStep.AWAITING_CONFIRM
            outcome = ActionOutcome.DELETIONS_DECLINED if declined else ActionOutcome.CANCELLED
            await self._cleanup(run)
        except Exception:  # Ein Lauf darf den Worker nie beenden.
            logger.exception("sync run failed unexpectedly")
            outcome = ActionOutcome.INTERNAL_ERROR
            await self._cleanup(run)
        finally:
            if self._target is not None:
                self._target.release_target(run.id)
        self._finish(run, outcome, reason)

    def _finish(self, run: _Run, outcome: ActionOutcome, reason: SyncReason | None) -> None:
        self._log.record(
            run.id, run.action, Trigger.DASHBOARD, outcome, reason=reason, adopted=run.adopted
        )
        finished_at = self._clock()
        self._last = SyncResult(
            kind=run.kind, outcome=outcome, reason=reason, finished_at=finished_at
        )
        self._history.append(
            SyncHistoryEntry(
                id=run.id,
                kind=run.kind,
                trigger=Trigger.DASHBOARD,
                started_at=run.started_at,
                finished_at=finished_at,
                result=outcome,
                reason=reason,
                woke_target=run.woke,
                containers_stopped=run.containers_stopped,
                adopted=run.adopted,
                shares=_history_shares(run.job),
            )
        )
        if self._run is run:
            self._run = None

    # --- Gemeinsame Schritte ---

    async def _order(
        self,
        run: _Run,
        order: GuardOrder,
        container: str | None = None,
        reply_seconds: float | None = None,
    ) -> GuardOrderResult:
        """Ein Auftrag; bei begin, stop und finish wartet ein Abbruch die Antwort noch ab.

        Sonst schriebe finish mitten in ein laufendes docker stop, oder ein offener Lauf bliebe
        unbemerkt; das Ergebnis zählt auch dann (_track). Endet der Worker, wartet er nicht.
        """
        if order not in _RUN_ORDERS:
            return await self._channel.order(order, container, reply_seconds)
        task = asyncio.ensure_future(self._channel.order(order, container, reply_seconds))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if self._closing:
                task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                self._track(run, order, await task)
            raise
        self._track(run, order, result)
        return result

    @staticmethod
    def _track(run: _Run, order: GuardOrder, result: GuardOrderResult) -> None:
        """Was eine Antwort am Lauf ändert: offen, angehaltene Container, geschlossen."""
        if order is GuardOrder.BEGIN and result is GuardOrderResult.OPENED:
            run.run_open = True
        elif order is GuardOrder.STOP and result is GuardOrderResult.STOPPED:
            run.containers_stopped += 1
        elif order is GuardOrder.FINISH:
            # Auch bei einem anderen Ergebnis: Den Lauf beendet dann der Wächter selbst.
            run.run_open = False

    async def _wake(self, run: _Run) -> None:
        if run.step is not SyncStep.WAKING:
            return
        if self._target is None:  # pragma: no cover - der Worker verbindet beide immer
            raise _End(ActionOutcome.INTERNAL_ERROR)
        run.woke = True
        await self._target.wake_for(run.id, run.action)

    async def _snapshot(self, run: _Run) -> None:
        run.step = SyncStep.SNAPSHOT
        run.snapshot_sent = True
        result = await self._order(run, GuardOrder.SNAPSHOT)
        if result is not GuardOrderResult.CREATED:
            run.snapshot_sent = False
            if result in _SNAPSHOT_OUTCOMES:
                raise _End(_SNAPSHOT_OUTCOMES[result])
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)
        details = active_details(await self._channel.observe())
        job = details.job
        if job is None or job.state is not GuardJobState.SNAPSHOT:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
        run.instance, run.guard_job, run.job = details.instance, job.id, job

    async def _prepare(self, run: _Run) -> tuple[GuardJob, GuardDetails]:
        run.step = SyncStep.PREPARING
        result = await self._order(run, GuardOrder.PREPARE)
        if result is not GuardOrderResult.PREPARING:
            await self._close_quietly(run)
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)
        job, details = await self._wait_for_prepare(run)
        if job is None:
            raise _End(ActionOutcome.JOB_TIMEOUT)
        run.job = job
        self._report = _report(run.kind, job, details.target_check, self._clock())
        return job, details

    async def _wait_for_prepare(self, run: _Run) -> tuple[GuardJob | None, GuardDetails]:
        """Wartet, bis prepare fertig ist; None nach der eigenen Frist (Wächter hing)."""
        deadline = self._monotonic() + self._timings.prepare_wait_seconds
        while True:
            details, job = await self._own_job(run)
            if job.state in _REPORT_STATES:
                return job, details
            if job.state not in _WAITING_STATES:
                raise _End(_ended_job_outcome(job))
            if self._monotonic() >= deadline:
                await self._close_quietly(run)
                return None, details
            await self._sleep(self._timings.job_poll_seconds)

    async def _own_job(self, run: _Run) -> tuple[GuardDetails, GuardJob]:
        """Frischer Status mit dem eigenen Job; sonst ist der Lauf verloren (Konzept 3.6)."""
        details = active_details(await self._channel.observe())
        job = details.job
        if details.instance != run.instance or job is None or job.id != run.guard_job:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_LOST)
        run.job = job
        if not job.open:
            # Der Job ist zu Ende; close ist nicht mehr nötig.
            run.guard_job = None
        return details, job

    async def _close_quietly(self, run: _Run) -> None:
        """close für den eigenen Job; scheitert es, schließt der Wächter ihn nach seiner Frist.

        Kam die Antwort auf snapshot nicht mehr an (Abbruch, Frist), zeigt der Status, ob ein
        Job nach dem Beginn dieses Laufs entstand; nur TwinRaid schreibt Aufträge.
        """
        if run.guard_job is None and not (run.snapshot_sent and await self._own_job_open(run)):
            return
        try:
            result = await asyncio.shield(self._channel.order(GuardOrder.CLOSE))
        except GuardOrderError, asyncio.CancelledError:
            logger.warning("sync: close was not confirmed by the source guard")
            return
        if result not in (GuardOrderResult.CLOSED, GuardOrderResult.NO_JOB):
            logger.warning("sync: close answered %s", result.value)
        run.guard_job = None
        run.snapshot_sent = False
        run.sending = False

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

    async def _cleanup(self, run: _Run) -> None:
        """Nach einem Abbruch oder Fehler: Container wieder starten, eigenen Job schließen."""
        if run.run_open:
            await self._finish_run(run)
        await self._close_quietly(run)

    async def _leave(self, run: _Run) -> None:
        """Der Worker endet: close für einen Job ohne Übertragung nur noch schreiben.

        Hat die Container-Phase finish schon geschrieben, folgt nichts mehr; ein zweiter
        Auftrag könnte finish überschreiben, bevor der Wächter es liest. Den Job schließt
        dann der Worker beim nächsten Start (Konzept 3.6).
        """
        if run.posted or run.sending or run.guard_job is None:
            return
        await self._post(run, GuardOrder.CLOSE)

    async def _post(self, run: _Run, order: GuardOrder) -> None:
        run.posted = True
        try:
            await self._channel.post(order)
        except Exception:  # noqa: BLE001 - beim Ende des Workers darf nichts hängen bleiben
            logger.warning("sync: %s could not be written to the source guard", order.value)

    # --- Probelauf ---

    async def _dry_run(self, run: _Run) -> None:
        await self._wake(run)
        await self._snapshot(run)
        job, details = await self._prepare(run)
        outcome, reason = evaluate_prepare(job, details.target_check)
        run.step = SyncStep.CLOSING
        await self._close_quietly(run)
        raise _End(outcome, reason)

    # --- Sync ---

    async def _sync(self, run: _Run) -> None:
        await self._wake(run)
        if self._config.sync.stop_containers:
            await self._container_phase(run)
        else:
            await self._snapshot(run)
        job, details = await self._prepare(run)
        outcome, reason = evaluate_prepare(job, details.target_check)
        if outcome is not ActionOutcome.DRY_RUN_SUCCESS:
            run.step = SyncStep.CLOSING
            await self._close_quietly(run)
            raise _End(outcome, reason)
        if job.state is GuardJobState.AWAITING_CONFIRM:
            await self._await_confirmation(run, job, details)
        await self._send(run)

    async def _container_phase(self, run: _Run) -> None:
        """begin, je Kandidat ping und stop, snapshot, finish (Konzept 4.3, Schritt 2)."""
        run.step = SyncStep.STOPPING_CONTAINERS
        report = await self._channel.observe()
        details = active_details(report)
        candidates = self._candidates(report, details)
        stop_reply = details.limits.stop_timeout_seconds + self._timings.stop_reply_extra_seconds
        result = await self._order(run, GuardOrder.BEGIN)
        if result is not GuardOrderResult.OPENED:
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)
        pinger = asyncio.create_task(self._pinger(run))
        try:
            await self._stop_containers(run, candidates, stop_reply)
            await self._snapshot(run)
        finally:
            pinger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pinger
            await self._finish_run(run)

    def _candidates(self, report: GuardReport, details: GuardDetails) -> list[str]:
        """Laufende Container der Erlaubnisliste, ohne never_stop und ohne TwinRaid selbst."""
        never = set(self._config.sync.never_stop)
        return [
            item.name
            for item in details.allowed
            if item.state is GuardContainerState.RUNNING
            and item.name not in never
            and item.name not in report.own_allowed
        ]

    async def _stop_containers(self, run: _Run, candidates: list[str], stop_reply: float) -> None:
        part = SyncPart.SOURCE_GUARD_STOP
        self._log.record(run.id, part, Trigger.AUTOMATIC, STARTED, run.action)
        outcome: ActionOutcome = ActionOutcome.INTERNAL_ERROR
        reason: GuardOrderResult | None = None
        try:
            for name in candidates:
                await self._ping(run)
                result = await self._order(run, GuardOrder.STOP, name, stop_reply)
                if result not in (GuardOrderResult.STOPPED, GuardOrderResult.NOT_RUNNING):
                    # Ein noch laufender Container ergäbe einen für ihn nur absturzkonsistenten
                    # Snapshot (Konzept 4.3, Schritt 2).
                    raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)
            # Hat der Totmannschalter den Lauf inzwischen geschlossen, endet der Sync hier.
            await self._ping(run)
            outcome = ActionOutcome.SUCCESS
        except _End as end:
            outcome = end.outcome
            reason = end.reason if isinstance(end.reason, GuardOrderResult) else None
            raise
        except GuardOrderError as failure:
            outcome = failure.outcome
            raise
        except asyncio.CancelledError:
            outcome = ActionOutcome.CANCELLED if run.cancel_requested else ActionOutcome.INTERRUPTED
            raise
        finally:
            self._log.record(
                run.id,
                part,
                Trigger.AUTOMATIC,
                outcome,
                run.action,
                reason=reason,
                containers=run.containers_stopped,
            )

    async def _ping(self, run: _Run) -> None:
        result = await self._order(run, GuardOrder.PING)
        if result is GuardOrderResult.NO_RUN:
            run.deadman = True
        if run.deadman:
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, GuardOrderResult.NO_RUN)
        if result is not GuardOrderResult.EXTENDED:
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)

    async def _pinger(self, run: _Run) -> None:
        """Solange der Lauf offen ist, alle 15 s ping (Konzept 4.3, Schritt 2)."""
        while True:
            await self._sleep(self._timings.ping_seconds)
            try:
                result = await self._channel.order(GuardOrder.PING)
            except GuardOrderError:
                continue
            if result is GuardOrderResult.NO_RUN:
                run.deadman = True
                return

    async def _finish_run(self, run: _Run) -> None:
        """finish: startet die angehaltenen Container wieder und schließt den Lauf."""
        if not run.run_open:
            return
        part = SyncPart.SOURCE_GUARD_FINISH
        run.step = SyncStep.STARTING_CONTAINERS
        self._log.record(run.id, part, Trigger.AUTOMATIC, STARTED, run.action)
        if self._closing:
            # Ausführen tut der Wächter finish auch ohne Antwort (Konzept 4.3).
            run.run_open = False
            await self._post(run, GuardOrder.FINISH)
            self._log.record(run.id, part, Trigger.AUTOMATIC, ActionOutcome.INTERRUPTED, run.action)
            return
        try:
            result = await self._order(run, GuardOrder.FINISH)
        except GuardOrderError as failure:
            # Dann startet der Totmannschalter des Wächters die Container.
            run.run_open = False
            self._log.record(run.id, part, Trigger.AUTOMATIC, failure.outcome, run.action)
            return
        outcome = ActionOutcome.SUCCESS
        if result in (GuardOrderResult.FINISHED, GuardOrderResult.NO_RUN):
            if await self._restart_failed(run):
                run.restart_failed = True
                outcome = ActionOutcome.CONTAINER_RESTART_FAILED
        else:
            outcome = ActionOutcome.SOURCE_GUARD_REFUSED
        reason = result if outcome is ActionOutcome.SOURCE_GUARD_REFUSED else None
        self._log.record(run.id, part, Trigger.AUTOMATIC, outcome, run.action, reason=reason)

    async def _restart_failed(self, run: _Run) -> bool:
        """Meldet der letzte Lauf start_failed, changed oder gone (Konzept 3.6, last_run)?"""
        try:
            details = active_details(await self._channel.observe())
        except GuardOrderError:
            return False
        last_run = details.last_run
        if last_run is None or last_run.opened_at < _seconds(run.started_at):
            return False
        return any(item.result in _RESTART_FAILED for item in last_run.containers)

    async def _await_confirmation(self, run: _Run, job: GuardJob, details: GuardDetails) -> None:
        """Wartet auf sync_confirm_deletions; sync_cancel lehnt ab (Konzept 4.3, Schritt 5)."""
        run.step = SyncStep.AWAITING_CONFIRM
        run.confirmation = SyncConfirmation(
            reasons=job.confirm_reasons,
            deletions=sum(item.deleted or 0 for item in job.datasets),
            max_deletions=details.limits.max_deletions,
            deadline=job.deadline,
        )
        deadline = self._monotonic() + self._timings.confirm_wait_seconds
        while not run.confirmed.is_set():
            if self._monotonic() >= deadline:
                await self._close_quietly(run)
                raise _End(ActionOutcome.CONFIRMATION_TIMEOUT)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(run.confirmed.wait(), self._timings.job_poll_seconds)
            if run.confirmed.is_set():
                break
            _details, job = await self._own_job(run)
            if job.state is not GuardJobState.AWAITING_CONFIRM:
                if job.result is GuardJobResult.JOB_TIMEOUT:
                    raise _End(ActionOutcome.CONFIRMATION_TIMEOUT)
                raise _End(_ended_job_outcome(job))
        run.confirmation = None
        run.step = SyncStep.SENDING
        result = await self._order(run, GuardOrder.CONFIRM)
        if result is not GuardOrderResult.CONFIRMED:
            await self._close_quietly(run)
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)

    async def _send(self, run: _Run) -> None:
        run.step = SyncStep.SENDING
        # Schon ab dem Schreiben: Endet der Worker, bevor die Antwort kommt, darf er die
        # vielleicht schon laufende Übertragung nicht mit close abbrechen (Konzept 4.3).
        run.sending = True
        result = await self._order(run, GuardOrder.SEND)
        if result is not GuardOrderResult.SENDING:
            run.sending = False
            await self._close_quietly(run)
            raise _End(ActionOutcome.SOURCE_GUARD_REFUSED, result)
        job = await self._wait_for_send(run)
        raise _End(*self._send_outcome(run, job))

    async def _wait_for_send(self, run: _Run) -> GuardJob:
        """Liest den Status alle 2 s bis zum Ende des Jobs, höchstens 12 h 1 min (4.3, 4.4)."""
        tracker = run.progress or ProgressTracker()
        run.progress = tracker
        deadline = self._monotonic() + self._timings.send_wait_seconds
        while True:
            _details, job = await self._own_job(run)
            done = job.state is GuardJobState.DONE
            tracker.update(self._monotonic(), self._clock(), job.datasets, done)
            if not job.open:
                run.sending = False
                self._update_report(job)
                return job
            if self._monotonic() >= deadline:
                await self._close_quietly(run)
                raise _End(ActionOutcome.JOB_TIMEOUT)
            await self._sleep(self._timings.job_poll_seconds)

    def _send_outcome(self, run: _Run, job: GuardJob) -> tuple[ActionOutcome, SyncReason | None]:
        """Ergebnis aus dem Job (Konzept 4.3, „Ergebnis“); ein Fehler beim Senden geht vor."""
        match job.result:
            case GuardJobResult.OK:
                if run.restart_failed:
                    return ActionOutcome.CONTAINER_RESTART_FAILED, None
                return ActionOutcome.SUCCESS, None
            case GuardJobResult.SEND_FAILED:
                special = _SEND_REASON_OUTCOMES.get(job.reason) if job.reason else None
                if special is not None:
                    return special, None
                return ActionOutcome.SEND_FAILED, job.reason
            case GuardJobResult.TARGET_REFUSED:
                return ActionOutcome.TARGET_REFUSED, job.reason
            case GuardJobResult.TARGET_MODIFIED:
                return ActionOutcome.TARGET_MODIFIED, None
            case GuardJobResult.JOB_TIMEOUT:
                return ActionOutcome.JOB_TIMEOUT, None
            case GuardJobResult.PREPARE_FAILED:
                return ActionOutcome.PREPARE_FAILED, None
            case _:
                return ActionOutcome.SOURCE_GUARD_LOST, None

    def _update_report(self, job: GuardJob) -> None:
        """Gesendete Bytes und Ergebnis je Share in den Bericht des Laufs übernehmen."""
        report = self._report
        if report is None or report.snapshot != job.snapshot:
            return
        results = {item.name: item for item in job.datasets}
        shares = tuple(
            share.model_copy(
                update={"sent": results[share.name].sent, "result": results[share.name].result}
            )
            if share.name in results
            else share
            for share in report.shares
        )
        self._report = report.model_copy(update={"shares": shares})

    # --- Übernommene Übertragung ---

    async def _adopted(self, run: _Run) -> None:
        job = await self._wait_for_send(run)
        raise _End(*self._send_outcome(run, job))

    # --- Nach dem Start des Workers ---

    async def run(self) -> None:
        """Räumt einmal auf, was ein früherer Worker beim Wächter offen ließ (Konzept 3.6)."""
        while True:
            try:
                report = await self._channel.observe()
            except Exception:  # Lesefehler: später erneut.
                logger.exception("reading the source guard for open runs and jobs failed")
                report = None
            if report is not None and report.status.state is GuardState.ACTIVE:
                await self._recover(report)
                return
            await self._sleep(self._timings.startup_poll_seconds)

    async def _recover(self, report: GuardReport) -> None:
        """Offener Lauf: finish; Job ohne Übertragung: close; Übertragung: übernehmen (4.3)."""
        details = report.status.details
        if details is None or self._run is not None:
            return
        if details.run is not None:
            try:
                result = await self._channel.order(GuardOrder.FINISH)
            except GuardOrderError as failure:
                logger.warning("finishing an open run of the source guard failed: %s", failure)
            else:
                logger.info("finished an open run of the source guard: %s", result.value)
        job = details.job
        if job is None:
            return
        if job.state in _STALE_STATES:
            try:
                result = await self._channel.order(GuardOrder.CLOSE)
            except GuardOrderError as failure:
                logger.warning("closing an open job of the source guard failed: %s", failure)
                return
            logger.info("closed an open job of the source guard after the start: %s", result.value)
        elif job.state is GuardJobState.SENDING and self._run is None:
            logger.info("adopted a transfer of the source guard that began before the start")
            run = _Run(
                new_action_id(),
                new_job_id(),
                SyncKind.SYNC,
                job.created_at,
                SyncStep.SENDING,
                instance=details.instance,
                guard_job=job.id,
                job=job,
                adopted=True,
                sending=True,
            )
            self._report = _report(SyncKind.SYNC, job, details.target_check, self._clock())
            self._start(run)


def _seconds(moment: datetime) -> datetime:
    """Zeitpunkte des Wächters haben ganze Sekunden."""
    return moment.replace(microsecond=0)


def _ended_job_outcome(job: GuardJob) -> ActionOutcome:
    """Job vorzeitig beendet: Fehler auf Klein, Frist oder Neustart des Wächters."""
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


def _history_shares(job: GuardJob | None) -> tuple[SyncHistoryShare, ...]:
    if job is None:
        return ()
    return tuple(
        SyncHistoryShare(
            name=item.name,
            mode=item.mode,
            deleted=item.deleted,
            modified=item.modified,
            added=item.added,
            renamed=item.renamed,
            size=item.size,
            sent=item.sent,
            result=item.result,
        )
        for item in job.datasets
    )


def _report(kind: SyncKind, job: GuardJob, check: GuardCheck | None, now: datetime) -> SyncReport:
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
        kind=kind,
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
                sent=item.sent,
                result=item.result,
            )
            for item in job.datasets
        ),
    )
