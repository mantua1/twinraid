"""Wächter mit Drehbuch für Tests des Probelaufs (Konzept 3.6 und 4.3).

Er ist Quelle des Status (report) und Kanal für Aufträge (observe, order) zugleich.
Ohne Vorgabe antwortet er wie source-guard.sh: snapshot legt einen Job an, prepare
startet die Prüfung, close schließt den Job. Was prepare liefert, bestimmt prepared.
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
    GuardDataset,
    GuardDetails,
    GuardError,
    GuardJob,
    GuardJobDataset,
    GuardJobResult,
    GuardJobState,
    GuardLastOrder,
    GuardLimits,
    GuardOrder,
    GuardOrderResult,
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
        "available": 600 * GIB,
        "size": 860 * GIB,
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

    def details(self) -> GuardDetails:
        now = datetime.now(UTC).replace(microsecond=0)
        return GuardDetails(
            version=2,
            started_at=now - timedelta(hours=1),
            allowed=(),
            run=None,
            last_run=None,
            run_allowed_at=None,
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
            SourceGuardStatus(state=self.state, error=self.error, details=self.details())
        )

    async def observe(self) -> GuardReport:
        if self.on_observe is not None:
            self.on_observe(self)
        self._advance()
        return self.report()

    def _advance(self) -> None:
        job, prepared = self.job, self.prepared
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

    async def order(self, order: GuardOrder) -> GuardOrderResult:
        self.orders.append(order)
        if order in self.failing:
            raise GuardOrderError(self.failing[order])
        if self.state is not GuardState.ACTIVE:
            raise GuardOrderError(ActionOutcome.SOURCE_GUARD_INACTIVE)
        self.last_seq += 1
        queue = self.answers.get(order)
        result = queue.pop(0) if queue else self._default(order)
        now = datetime.now(UTC).replace(microsecond=0)
        self._last = GuardLastOrder(seq=self.last_seq, order=order, result=result, at=now)
        if order is self.hold:
            await self.release.wait()
        return result

    def _default(self, order: GuardOrder) -> GuardOrderResult:
        now = datetime.now(UTC).replace(microsecond=0)
        job = self.job
        match order:
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
