"""Simulierter Lauf und Job des Wächters für Probelauf und Sync (Konzept 6.4, ab v0.5.1).

Feste Zeiten und Zahlen: snapshot antwortet nach 1 s, prepare dauert 6 s, jedes stop 2 s,
gesendet wird mit 25 MiB/s. Bericht je Share: inkrementell vom Snapshot, 12 gelöscht,
34 geändert, 56 neu, 2 umbenannt, referenced 2 GiB (Basis 2,1 GiB), Größe 350 MiB; Spiegel
mit 5 Snapshots; auf Groß 1000 GiB von 2000 GiB frei, Reserve 15 %. Die Pause zwischen zwei
Läufen beträgt in der Simulation 2 min. Fehler mit twinraid-admin mock-fault:
sync-deletions, sync-empty-source, sync-target-refused, target-exists, target-clone,
sync-send-error (die letzte Share scheitert beim Senden) und sync-slow (1 MiB/s).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheck,
    GuardCheckMirror,
    GuardCheckResult,
    GuardCheckSummary,
    GuardConfirmReason,
    GuardContainer,
    GuardContainerState,
    GuardDatasetProblem,
    GuardDatasetResult,
    GuardJob,
    GuardJobDataset,
    GuardJobReason,
    GuardJobResult,
    GuardJobState,
    GuardLastOrder,
    GuardLastRun,
    GuardOrder,
    GuardOrderResult,
    GuardRestart,
    GuardRestartResult,
    GuardRun,
    GuardRunEnd,
)

MIB = 1024**2
GIB = 1024**3
# Wie in source-guard.conf und mirror.conf (Standardwerte).
MAX_DELETIONS = 1000
RESERVE_PERCENT = 15
# Groß: glatte, erfundene Zahlen (Konzept 6.4).
POOL_SIZE = 2000 * GIB
POOL_AVAILABLE = 1000 * GIB
LOW_AVAILABLE = 100 * GIB
# Fristen des Wächters (Konzept 3.6): ohne prepare bzw. send oder close 30 min, Senden 12 h.
JOB_DEADLINE = timedelta(minutes=30)
SEND_DEADLINE = timedelta(hours=12)
# Lauf: Herzschlag 120 s, Höchstzeit 20 min (Standard von source-guard.conf).
HEARTBEAT = timedelta(seconds=120)
MAX_RUN = timedelta(minutes=20)
MIRROR_SNAPSHOTS = 5
SHARE_SIZE = 350 * MIB


@dataclass(frozen=True)
class MockJobTimings:
    snapshot_seconds: float = 1.0
    prepare_seconds: float = 6.0
    stop_seconds: float = 2.0
    # Bytes je Sekunde beim Senden; mit sync-slow die langsame Rate.
    send_rate: float = 25 * MIB
    slow_send_rate: float = 1 * MIB
    run_interval: timedelta = timedelta(minutes=2)


def _empty(name: str) -> GuardJobDataset:
    return GuardJobDataset(
        name=name,
        mode=None,
        base_type=None,
        problem=None,
        deleted=None,
        modified=None,
        added=None,
        renamed=None,
        referenced=None,
        base_referenced=None,
        size=None,
        sent=None,
        result=None,
    )


class MockJobs:
    """Auftragsnummer, Lauf, Job und letztes check des simulierten Wächters."""

    def __init__(
        self,
        datasets: tuple[str, ...],
        containers: tuple[str, ...],
        fault: Callable[[str], bool],
        clock: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
        timings: MockJobTimings | None = None,
    ) -> None:
        self._datasets = datasets
        self._fault = fault
        self._clock = clock
        self._monotonic = monotonic
        self.timings = timings or MockJobTimings()
        self.last_seq = 0
        self.last_order: GuardLastOrder | None = None
        self.job: GuardJob | None = None
        self.check: GuardCheck | None = None
        self.run: GuardRun | None = None
        self.last_run: GuardLastRun | None = None
        self.run_allowed_at: datetime | None = None
        # Laufen die freigegebenen Container gerade?
        self.containers: dict[str, bool] = dict.fromkeys(containers, True)
        # Gehaltene Basis je Dataset nach einem erfolgreichen Senden.
        self.bases: dict[str, datetime] = {}
        self._prepare_until: float | None = None
        self._send_started: float | None = None
        self._send_rate = self.timings.send_rate

    def allowed(self) -> tuple[GuardContainer, ...]:
        return tuple(
            GuardContainer(
                name=name,
                state=GuardContainerState.RUNNING if running else GuardContainerState.STOPPED,
            )
            for name, running in self.containers.items()
        )

    def will_stop(self, name: str) -> bool:
        """stop hält diesen Container jetzt an (dauert dann stop_seconds)."""
        return self.run is not None and self.containers.get(name, False)

    def advance(self) -> None:
        """Fristen, Ende von prepare, Fortschritt beim Senden, Totmannschalter."""
        now = self._clock()
        run = self.run
        if run is not None and (now > run.heartbeat_deadline or now > run.max_deadline):
            reason = (
                GuardRunEnd.DEADMAN_MAX if now > run.max_deadline else GuardRunEnd.DEADMAN_HEARTBEAT
            )
            self._end_run(now, reason)
        if self.run_allowed_at is not None and now >= self.run_allowed_at:
            self.run_allowed_at = None
        job = self.job
        if job is None:
            return
        if (
            job.state is GuardJobState.PREPARING
            and self._prepare_until is not None
            and self._monotonic() >= self._prepare_until
        ):
            self._prepare_until = None
            self.job, self.check = self._prepared(job, now)
        elif job.state is GuardJobState.SENDING:
            self.job = self._sending(job, now)
        elif job.open and job.deadline is not None and now >= job.deadline:
            self.job = job.model_copy(
                update={
                    "state": GuardJobState.CLOSED,
                    "result": GuardJobResult.JOB_TIMEOUT,
                    "deadline": None,
                }
            )

    def apply(self, order: GuardOrder, container: str | None = None) -> GuardOrderResult:
        """Führt einen Auftrag aus wie source-guard.sh."""
        self.advance()
        self.last_seq += 1
        now = self._clock()
        result = self._result(order, container, now)
        self.last_order = GuardLastOrder(seq=self.last_seq, order=order, result=result, at=now)
        return result

    def _result(self, order: GuardOrder, container: str | None, now: datetime) -> GuardOrderResult:
        job = self.job
        open_job = job is not None and job.open
        match order:
            case GuardOrder.BEGIN:
                if self.run is not None:
                    return GuardOrderResult.RUN_OPEN
                if open_job:
                    return GuardOrderResult.JOB_OPEN
                if self.run_allowed_at is not None:
                    return GuardOrderResult.TOO_SOON
                self.run = GuardRun(
                    opened_at=now.replace(microsecond=0),
                    heartbeat_deadline=now + HEARTBEAT,
                    max_deadline=now + MAX_RUN,
                    stopped=(),
                )
                return GuardOrderResult.OPENED
            case GuardOrder.STOP:
                return self._stop(container or "", now)
            case GuardOrder.PING:
                if self.run is None:
                    return GuardOrderResult.NO_RUN
                self.run = self.run.model_copy(update={"heartbeat_deadline": now + HEARTBEAT})
                return GuardOrderResult.EXTENDED
            case GuardOrder.FINISH:
                if self.run is None:
                    return GuardOrderResult.NO_RUN
                self._end_run(now, GuardRunEnd.FINISH)
                return GuardOrderResult.FINISHED
            case GuardOrder.SNAPSHOT:
                if open_job:
                    return GuardOrderResult.JOB_OPEN
                stamp = now.strftime("%Y%m%dT%H%M%SZ")
                self.job = GuardJob(
                    id=self.last_seq,
                    state=GuardJobState.SNAPSHOT,
                    result=None,
                    reason=None,
                    snapshot=f"twinraid-{stamp}",
                    created_at=now.replace(microsecond=0),
                    deadline=now + JOB_DEADLINE,
                    confirmed=False,
                    confirm_reasons=(),
                    send_ready=None,
                    blockers=(),
                    datasets=tuple(_empty(name) for name in self._datasets),
                )
                return GuardOrderResult.CREATED
            case GuardOrder.PREPARE:
                if job is None or not open_job:
                    return GuardOrderResult.NO_JOB
                if job.state is not GuardJobState.SNAPSHOT:
                    return GuardOrderResult.WRONG_STATE
                if self.run is not None:
                    return GuardOrderResult.RUN_OPEN
                self.job = job.model_copy(
                    update={"state": GuardJobState.PREPARING, "deadline": now + JOB_DEADLINE}
                )
                self._prepare_until = self._monotonic() + self.timings.prepare_seconds
                return GuardOrderResult.PREPARING
            case GuardOrder.CONFIRM:
                if job is None or not open_job:
                    return GuardOrderResult.NO_JOB
                if job.state is not GuardJobState.AWAITING_CONFIRM:
                    return GuardOrderResult.NOT_NEEDED
                self.job = job.model_copy(
                    update={"state": GuardJobState.PREPARED, "confirmed": True}
                )
                return GuardOrderResult.CONFIRMED
            case GuardOrder.SEND:
                if job is None or not open_job:
                    return GuardOrderResult.NO_JOB
                if job.state is not GuardJobState.PREPARED:
                    return GuardOrderResult.WRONG_STATE
                if job.send_ready is not True:
                    return GuardOrderResult.NOT_READY
                slow = self._fault("sync-slow")
                self._send_rate = self.timings.slow_send_rate if slow else self.timings.send_rate
                self._send_started = self._monotonic()
                self.job = job.model_copy(
                    update={"state": GuardJobState.SENDING, "deadline": now + SEND_DEADLINE}
                )
                return GuardOrderResult.SENDING
            case GuardOrder.CLOSE:
                if job is None or not open_job:
                    return GuardOrderResult.NO_JOB
                self._prepare_until = None
                self._send_started = None
                self.job = job.model_copy(
                    update={
                        "state": GuardJobState.CLOSED,
                        "result": GuardJobResult.CLOSED,
                        "deadline": None,
                    }
                )
                return GuardOrderResult.CLOSED

    def _stop(self, name: str, now: datetime) -> GuardOrderResult:
        run = self.run
        if run is None:
            return GuardOrderResult.NO_RUN
        if name not in self.containers:
            return GuardOrderResult.NOT_ALLOWED
        # Wie source-guard.sh ab Version 3: stop setzt die Herzschlag-Frist neu.
        run = run.model_copy(update={"heartbeat_deadline": now + HEARTBEAT})
        if not self.containers[name]:
            self.run = run
            return GuardOrderResult.NOT_RUNNING
        self.containers[name] = False
        self.run = run.model_copy(update={"stopped": (*run.stopped, name)})
        return GuardOrderResult.STOPPED

    def _end_run(self, now: datetime, reason: GuardRunEnd) -> None:
        """Startet die angehaltenen Container in umgekehrter Reihenfolge wieder."""
        run = self.run
        if run is None:
            return
        failed = self._fault("source-guard-start-failed")
        results: list[GuardRestartResult] = []
        for name in reversed(run.stopped):
            if failed:
                results.append(GuardRestartResult(name=name, result=GuardRestart.START_FAILED))
                continue
            self.containers[name] = True
            results.append(GuardRestartResult(name=name, result=GuardRestart.STARTED))
        self.last_run = GuardLastRun(
            opened_at=run.opened_at,
            closed_at=now.replace(microsecond=0),
            reason=reason,
            containers=tuple(results),
        )
        self.run = None
        self.run_allowed_at = (now + self.timings.run_interval).replace(microsecond=0)

    def _sending(self, job: GuardJob, now: datetime) -> GuardJob:
        """Fortschritt: die Shares nacheinander mit der Rate; sync-send-error trifft die letzte."""
        started = self._send_started
        if started is None:  # pragma: no cover - SENDING kommt nur über send
            return job
        budget = (self._monotonic() - started) * self._send_rate
        datasets: list[GuardJobDataset] = []
        failed = False
        last = len(job.datasets) - 1
        for index, item in enumerate(job.datasets):
            size = item.size or 0
            if failed or budget <= 0:
                datasets.append(
                    item.model_copy(
                        update={"sent": 0, "result": GuardDatasetResult.SKIPPED if failed else None}
                    )
                )
                continue
            if index == last and self._fault("sync-send-error"):
                failed = True
                datasets.append(
                    item.model_copy(update={"sent": 0, "result": GuardDatasetResult.SEND_FAILED})
                )
                continue
            if budget >= size:
                budget -= size
                datasets.append(
                    item.model_copy(update={"sent": size, "result": GuardDatasetResult.OK})
                )
                self.bases[item.name] = job.created_at
                continue
            datasets.append(item.model_copy(update={"sent": int(budget), "result": None}))
            budget = 0
        if failed:
            self._send_started = None
            return job.model_copy(
                update={
                    "state": GuardJobState.FAILED,
                    "result": GuardJobResult.SEND_FAILED,
                    "reason": GuardJobReason.UNREACHABLE,
                    "deadline": None,
                    "datasets": tuple(datasets),
                }
            )
        if all(item.result is GuardDatasetResult.OK for item in datasets):
            self._send_started = None
            return job.model_copy(
                update={
                    "state": GuardJobState.DONE,
                    "result": GuardJobResult.OK,
                    "deadline": None,
                    "datasets": tuple(datasets),
                }
            )
        if job.deadline is not None and now >= job.deadline:
            self._send_started = None
            return job.model_copy(
                update={
                    "state": GuardJobState.CLOSED,
                    "result": GuardJobResult.JOB_TIMEOUT,
                    "deadline": None,
                    "datasets": tuple(datasets),
                }
            )
        return job.model_copy(update={"datasets": tuple(datasets)})

    def _prepared(self, job: GuardJob, now: datetime) -> tuple[GuardJob, GuardCheck]:
        foreign = self._fault("target-exists")
        clones = 1 if self._fault("target-clone") else 0
        available = LOW_AVAILABLE if self._fault("sync-target-refused") else POOL_AVAILABLE
        deleted = 1500 if self._fault("sync-deletions") else 12
        referenced = 300 * MIB if self._fault("sync-empty-source") else 2 * GIB
        base_referenced = 21 * GIB // 10
        datasets: list[GuardJobDataset] = []
        mirrors: list[GuardCheckMirror] = []
        total = 0
        deletions = 0
        shrunk = False
        for item in job.datasets:
            if foreign:
                datasets.append(
                    _empty(item.name).model_copy(
                        update={
                            "problem": GuardDatasetProblem.TARGET_EXISTS,
                            "referenced": referenced,
                        }
                    )
                )
                mirrors.append(
                    GuardCheckMirror(
                        name=item.name,
                        state="foreign",
                        props=None,
                        count=None,
                        newest_created=None,
                        newest_received=None,
                        oldest_created=None,
                    )
                )
                continue
            total += SHARE_SIZE
            deletions += deleted
            shrunk = shrunk or referenced * 100 < base_referenced * 50
            datasets.append(
                item.model_copy(
                    update={
                        "mode": "incremental",
                        "base_type": "snapshot",
                        "deleted": deleted,
                        "modified": 34,
                        "added": 56,
                        "renamed": 2,
                        "referenced": referenced,
                        "base_referenced": base_referenced,
                        "size": SHARE_SIZE,
                    }
                )
            )
            newest = self.bases.get(item.name, now - timedelta(days=1))
            mirrors.append(
                GuardCheckMirror(
                    name=item.name,
                    state="mirror",
                    props="ok",
                    count=MIRROR_SNAPSHOTS,
                    newest_created=newest,
                    newest_received=newest + timedelta(minutes=12),
                    oldest_created=now - timedelta(days=29),
                )
            )
        # Reihenfolge wie in source-guard.sh: Groß, dann je Dataset, zuletzt der Platz.
        blockers: list[GuardBlocker] = []
        if clones:
            blockers.append(GuardBlocker.CLONE_ACTIVE)
        if foreign:
            blockers.append(GuardBlocker.TARGET_EXISTS)
        if (available - total) * 100 < RESERVE_PERCENT * POOL_SIZE:
            blockers.append(GuardBlocker.SPACE_LOW)
        reasons: list[GuardConfirmReason] = []
        if shrunk:
            reasons.append(GuardConfirmReason.SHRUNK)
        if deletions > MAX_DELETIONS:
            reasons.append(GuardConfirmReason.DELETIONS)
        state = GuardJobState.AWAITING_CONFIRM if reasons else GuardJobState.PREPARED
        prepared = job.model_copy(
            update={
                "state": state,
                "deadline": now + JOB_DEADLINE,
                "confirm_reasons": tuple(reasons),
                "send_ready": not blockers,
                "blockers": tuple(blockers),
                "datasets": tuple(datasets),
            }
        )
        check = GuardCheck(
            at=now.replace(microsecond=0),
            result=GuardCheckResult.OK,
            summary=GuardCheckSummary(
                root="ok",
                conf="ok",
                deleg="ok",
                receive="idle",
                clones=clones,
                available=available,
                size=POOL_SIZE,
                reserve_percent=RESERVE_PERCENT,
                keep_days=30,
                min_interval_minutes=10,
            ),
            mirrors=tuple(mirrors),
        )
        return prepared, check
