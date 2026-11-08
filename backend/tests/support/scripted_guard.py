"""Wächter mit Drehbuch für Tests von Probelauf und Sync (Konzept 3.6 und 4.3).

Er ist Quelle des Status (report) und Kanal für Aufträge (observe, order, post) zugleich.
Ohne Vorgabe antwortet er wie source-guard.sh: begin öffnet einen Lauf, stop hält einen
freigegebenen Container an, finish startet ihn wieder, snapshot legt einen Job an, prepare
startet die Prüfung, confirm gibt frei, send sendet, close schließt den Job. Was prepare
liefert, bestimmt prepared, was send liefert, bestimmt sent.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from twinraid.integrations.guard_orders import GuardOrderError
from twinraid.integrations.source_guard import GuardReport
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheck,
    GuardCheckMirror,
    GuardCheckResult,
    GuardCheckSummary,
    GuardConfirmReason,
    GuardContainer,
    GuardContainerState,
    GuardDataset,
    GuardDatasetResult,
    GuardDetails,
    GuardError,
    GuardJob,
    GuardJobDataset,
    GuardJobReason,
    GuardJobResult,
    GuardJobState,
    GuardLastOrder,
    GuardLastRun,
    GuardLimits,
    GuardOrder,
    GuardOrderResult,
    GuardRestart,
    GuardRestartResult,
    GuardRun,
    GuardRunEnd,
    GuardSelfDataset,
    GuardState,
    GuardZfs,
    GuardZfsState,
    SourceGuardStatus,
)

SHARES = ("appdata", "dokumente", "medien")
GIB = 1024**3
LIMITS = GuardLimits(
    max_stop_minutes=20,
    heartbeat_timeout_seconds=120,
    stop_timeout_seconds=60,
    min_run_interval_minutes=30,
    min_snapshot_interval_minutes=30,
    max_deletions=1000,
    snapshot_keep_days=7,
    snapshot_reserve_percent=15,
)


def ok_check(shares: tuple[str, ...] = SHARES, **summary: object) -> GuardCheck:
    values: dict[str, object] = {
        "root": "ok",
        "conf": "ok",
        "deleg": "ok",
        "receive": "idle",
        "clones": 0,
        "available": 1000 * GIB,
        "size": 2000 * GIB,
        "reserve_percent": 15,
        "keep_days": 30,
        "min_interval_minutes": 10,
    }
    values.update(summary)
    return GuardCheck(
        at=datetime.now(UTC).replace(microsecond=0),
        result=GuardCheckResult.OK,
        summary=GuardCheckSummary.model_validate(values),
        mirrors=tuple(
            GuardCheckMirror(
                name=name,
                state="mirror",
                props="ok",
                count=3,
                newest_created=None,
                newest_received=None,
                oldest_created=None,
            )
            for name in shares
        ),
    )


def failed_check(result: GuardCheckResult) -> GuardCheck:
    return GuardCheck(
        at=datetime.now(UTC).replace(microsecond=0), result=result, summary=None, mirrors=()
    )


def dataset_report(name: str, **values: object) -> GuardJobDataset:
    data: dict[str, object] = {
        "name": name,
        "mode": "incremental",
        "base_type": "snapshot",
        "problem": None,
        "deleted": 1,
        "modified": 2,
        "added": 3,
        "renamed": 0,
        "referenced": GIB,
        "base_referenced": GIB,
        "size": 1000,
        "sent": None,
        "result": None,
    }
    data.update(values)
    return GuardJobDataset.model_validate(data)


@dataclass
class Prepared:
    """Was prepare liefert: Zustand des Jobs, Freigabe, Sperren und check."""

    state: GuardJobState = GuardJobState.PREPARED
    result: GuardJobResult | None = None
    send_ready: bool | None = True
    blockers: tuple[GuardBlocker, ...] = ()
    confirm_reasons: tuple[GuardConfirmReason, ...] = ()
    check: GuardCheck | None = field(default_factory=ok_check)
    # So oft liefert observe noch preparing, bevor das Ergebnis kommt.
    delay: int = 1


@dataclass
class Sent:
    """Was send liefert: Ergebnis des Jobs, Grund und je Share das Ergebnis."""

    result: GuardJobResult = GuardJobResult.OK
    reason: GuardJobReason | None = None
    # So oft liefert observe noch sending (mit wachsendem Fortschritt), bevor das Ende kommt.
    delay: int = 1
    # Ergebnis je Share; ohne Angabe ok bzw. bei einem Fehler send_failed.
    results: dict[str, GuardDatasetResult] = field(default_factory=dict)


@dataclass
class ScriptedGuard:
    shares: tuple[str, ...] = SHARES
    state: GuardState = GuardState.ACTIVE
    error: GuardError | None = None
    zfs_state: GuardZfsState = GuardZfsState.OK
    self_dataset: GuardSelfDataset = GuardSelfDataset.FOUND
    instance: str = "0123456789abcdef"
    last_seq: int = 0
    job: GuardJob | None = None
    check: GuardCheck | None = None
    prepared: Prepared | None = field(default_factory=Prepared)
    sent: Sent | None = field(default_factory=Sent)
    # Freigegebene Container (laufend) und Lauf des Wächters.
    allowed: tuple[GuardContainer, ...] = ()
    run: GuardRun | None = None
    last_run: GuardLastRun | None = None
    run_allowed_at: datetime | None = None
    # Ergebnis je Container beim Ende eines Laufs; ohne Angabe started.
    restarts: dict[str, GuardRestart] = field(default_factory=dict)
    # Aufträge mit Container und Wartezeit, wie sie der Worker schickt.
    calls: list[tuple[GuardOrder, str | None, float | None]] = field(default_factory=list)
    posted: list[GuardOrder] = field(default_factory=list)
    # Einträge in allowed mit der eigenen Container-ID (GuardReport.own_allowed).
    own: frozenset[str] = frozenset()
    # Vorgegebene Antworten je Auftrag (der Reihe nach), sonst wie source-guard.sh.
    answers: dict[GuardOrder, list[GuardOrderResult]] = field(default_factory=dict)
    # Auftrag, der mit GuardOrderError scheitert.
    failing: dict[GuardOrder, ActionOutcome] = field(default_factory=dict)
    # Wartet bei diesem Auftrag, nachdem er ausgeführt ist, bis release gesetzt wird.
    hold: GuardOrder | None = None
    release: asyncio.Event = field(default_factory=asyncio.Event)
    # Wird bei jedem observe aufgerufen (etwa um den Wächter mitten im Lauf zu ändern).
    on_observe: Callable[[ScriptedGuard], None] | None = None
    orders: list[GuardOrder] = field(default_factory=list)
    _last: GuardLastOrder | None = None
    _waits: int = 0
    _send_waits: int = 0

    def details(self) -> GuardDetails:
        now = datetime.now(UTC).replace(microsecond=0)
        return GuardDetails(
            version=2,
            started_at=now - timedelta(hours=1),
            allowed=self.allowed,
            run=self.run,
            last_run=self.last_run,
            run_allowed_at=self.run_allowed_at,
            zfs=GuardZfs(
                state=self.zfs_state,
                pool_used=100 * GIB,
                pool_available=700 * GIB,
                datasets=tuple(
                    GuardDataset(name=name, present=True, referenced=GIB, compression="on")
                    for name in self.shares
                ),
                self_dataset=self.self_dataset,
            ),
            limits=LIMITS,
            target_configured=True,
            target_port=22,
            rejected_count=0,
            instance=self.instance,
            last_seq=self.last_seq,
            last_order=self._last,
            target_key=True,
            target_known_host=True,
            target_check=self.check,
            job=self.job,
        )

    def report(self) -> GuardReport:
        if self.state is GuardState.INACTIVE:
            return GuardReport(SourceGuardStatus(state=GuardState.INACTIVE))
        return GuardReport(
            SourceGuardStatus(state=self.state, error=self.error, details=self.details()),
            own_allowed=self.own,
        )

    async def observe(self) -> GuardReport:
        if self.on_observe is not None:
            self.on_observe(self)
        self._advance()
        return self.report()

    def _advance(self) -> None:
        job = self.job
        if job is not None and job.state is GuardJobState.SENDING:
            self._advance_send(job)
            return
        prepared = self.prepared
        if job is None or job.state is not GuardJobState.PREPARING or prepared is None:
            return
        if self._waits < prepared.delay:
            self._waits += 1
            return
        self.check = prepared.check
        self.job = job.model_copy(
            update={
                "state": prepared.state,
                "result": prepared.result,
                "send_ready": prepared.send_ready,
                "blockers": prepared.blockers,
                "confirm_reasons": prepared.confirm_reasons,
                "datasets": tuple(dataset_report(name) for name in self.shares),
            }
        )

    def _advance_send(self, job: GuardJob) -> None:
        sent = self.sent
        if sent is None:
            return
        self._send_waits += 1
        if self._send_waits <= sent.delay:
            # Fortschritt: je Abfrage ein Teil der ersten Share.
            first, *rest = job.datasets
            part = min((first.size or 0), (first.sent or 0) + (first.size or 0) // 4)
            self.job = job.model_copy(
                update={"datasets": (first.model_copy(update={"sent": part}), *rest)}
            )
            return
        ok = sent.result is GuardJobResult.OK
        default = GuardDatasetResult.OK if ok else GuardDatasetResult.SEND_FAILED
        datasets = tuple(
            item.model_copy(
                update={
                    "sent": item.size
                    if sent.results.get(item.name, default) is GuardDatasetResult.OK
                    else 0,
                    "result": sent.results.get(item.name, default),
                }
            )
            for item in job.datasets
        )
        self.job = job.model_copy(
            update={
                "state": GuardJobState.DONE if ok else GuardJobState.FAILED,
                "result": sent.result,
                "reason": sent.reason,
                "datasets": datasets,
            }
        )

    async def order(
        self,
        order: GuardOrder,
        container: str | None = None,
        reply_seconds: float | None = None,
    ) -> GuardOrderResult:
        self.orders.append(order)
        self.calls.append((order, container, reply_seconds))
        if order in self.failing:
            raise GuardOrderError(self.failing[order])
        if self.state is not GuardState.ACTIVE:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_INACTIVE)
        self.last_seq += 1
        queue = self.answers.get(order)
        result = queue.pop(0) if queue else self._default(order, container)
        now = datetime.now(UTC).replace(microsecond=0)
        self._last = GuardLastOrder(seq=self.last_seq, order=order, result=result, at=now)
        if order is self.hold:
            await self.release.wait()
        return result

    async def post(self, order: GuardOrder) -> None:
        self.posted.append(order)
        if self.state is not GuardState.ACTIVE:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_INACTIVE)
        self.last_seq += 1
        self._default(order, None)

    def _container(self, name: str) -> GuardContainer | None:
        return next((item for item in self.allowed if item.name == name), None)

    def _set_container(self, name: str, state: GuardContainerState) -> None:
        self.allowed = tuple(
            item.model_copy(update={"state": state}) if item.name == name else item
            for item in self.allowed
        )

    def _default(self, order: GuardOrder, container: str | None = None) -> GuardOrderResult:
        now = datetime.now(UTC).replace(microsecond=0)
        job = self.job
        match order:
            case GuardOrder.BEGIN:
                if self.run is not None:
                    return GuardOrderResult.RUN_OPEN
                self.run = GuardRun(
                    opened_at=now,
                    heartbeat_deadline=now + timedelta(minutes=2),
                    max_deadline=now + timedelta(minutes=20),
                    stopped=(),
                )
                return GuardOrderResult.OPENED
            case GuardOrder.STOP:
                if self.run is None:
                    return GuardOrderResult.NO_RUN
                item = self._container(container or "")
                if item is None:
                    return GuardOrderResult.NOT_ALLOWED
                if item.state is not GuardContainerState.RUNNING:
                    return GuardOrderResult.NOT_RUNNING
                self._set_container(item.name, GuardContainerState.STOPPED)
                self.run = self.run.model_copy(update={"stopped": (*self.run.stopped, item.name)})
                return GuardOrderResult.STOPPED
            case GuardOrder.PING:
                return GuardOrderResult.EXTENDED if self.run else GuardOrderResult.NO_RUN
            case GuardOrder.FINISH:
                run = self.run
                if run is None:
                    return GuardOrderResult.NO_RUN
                results = []
                for name in reversed(run.stopped):
                    result = self.restarts.get(name, GuardRestart.STARTED)
                    if result is GuardRestart.STARTED:
                        self._set_container(name, GuardContainerState.RUNNING)
                    results.append(GuardRestartResult(name=name, result=result))
                self.last_run = GuardLastRun(
                    opened_at=run.opened_at,
                    closed_at=now,
                    reason=GuardRunEnd.FINISH,
                    containers=tuple(results),
                )
                self.run = None
                return GuardOrderResult.FINISHED
            case GuardOrder.CONFIRM:
                if job is None or not job.open:
                    return GuardOrderResult.NO_JOB
                if job.state is not GuardJobState.AWAITING_CONFIRM:
                    return GuardOrderResult.NOT_NEEDED
                self.job = job.model_copy(
                    update={"state": GuardJobState.PREPARED, "confirmed": True}
                )
                return GuardOrderResult.CONFIRMED
            case GuardOrder.SEND:
                if job is None or not job.open:
                    return GuardOrderResult.NO_JOB
                if job.state is not GuardJobState.PREPARED:
                    return GuardOrderResult.WRONG_STATE
                if job.send_ready is not True:
                    return GuardOrderResult.NOT_READY
                self._send_waits = 0
                self.job = job.model_copy(update={"state": GuardJobState.SENDING})
                return GuardOrderResult.SENDING
            case GuardOrder.SNAPSHOT:
                if job is not None and job.open:
                    return GuardOrderResult.JOB_OPEN
                self.job = GuardJob(
                    id=self.last_seq,
                    state=GuardJobState.SNAPSHOT,
                    result=None,
                    reason=None,
                    snapshot="twinraid-20261021T120000Z",
                    created_at=now,
                    deadline=None,
                    confirmed=False,
                    confirm_reasons=(),
                    send_ready=None,
                    blockers=(),
                    datasets=(),
                )
                return GuardOrderResult.CREATED
            case GuardOrder.PREPARE:
                if job is None:
                    return GuardOrderResult.NO_JOB
                if self.run is not None:
                    return GuardOrderResult.RUN_OPEN
                self._waits = 0
                self.job = job.model_copy(update={"state": GuardJobState.PREPARING})
                return GuardOrderResult.PREPARING
            case GuardOrder.CLOSE:
                if job is None or not job.open:
                    return GuardOrderResult.NO_JOB
                self.job = job.model_copy(
                    update={"state": GuardJobState.CLOSED, "result": GuardJobResult.CLOSED}
                )
                return GuardOrderResult.CLOSED
        return GuardOrderResult.NOT_SUPPORTED
