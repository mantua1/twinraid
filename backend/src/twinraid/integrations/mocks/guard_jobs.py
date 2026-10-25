"""Simulierter Job des Wächters für den Probelauf (Konzept 6.4, ab v0.5.1).

Feste Zeiten und Zahlen: snapshot antwortet nach 1 s, prepare dauert 6 s. Bericht je
Share: inkrementell vom Snapshot, 12 gelöscht, 34 geändert, 56 neu, 2 umbenannt,
referenced 2 GiB (Basis 2,1 GiB), Größe 350 MiB; Spiegel mit 5 Snapshots; auf Groß
600 GiB von 860 GiB frei, Reserve 15 %. Fehler mit twinraid-admin mock-fault:
sync-deletions, sync-empty-source, sync-target-refused, target-exists und target-clone.
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
    GuardDatasetProblem,
    GuardJob,
    GuardJobDataset,
    GuardJobResult,
    GuardJobState,
    GuardLastOrder,
    GuardOrder,
    GuardOrderResult,
)

MIB = 1024**2
GIB = 1024**3
# Wie in source-guard.conf und mirror.conf (Standardwerte).
MAX_DELETIONS = 1000
RESERVE_PERCENT = 15
POOL_SIZE = 860 * GIB
# Fristen des Wächters (Konzept 3.6): ohne prepare bzw. send oder close 30 min.
JOB_DEADLINE = timedelta(minutes=30)
MIRROR_SNAPSHOTS = 5


@dataclass(frozen=True)
class MockJobTimings:
    snapshot_seconds: float = 1.0
    prepare_seconds: float = 6.0


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
    """Auftragsnummer, letzter Auftrag, Job und letztes check des simulierten Wächters."""

    def __init__(
        self,
        datasets: tuple[str, ...],
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
        self._prepare_until: float | None = None

    def advance(self) -> None:
        """prepare fertig nach seiner Dauer; ein Job ohne Antwort endet nach seiner Frist."""
        job = self.job
        if job is None:
            return
        now = self._clock()
        if (
            job.state is GuardJobState.PREPARING
            and self._prepare_until is not None
            and self._monotonic() >= self._prepare_until
        ):
            self._prepare_until = None
            self.job, self.check = self._prepared(job, now)
        elif job.open and job.deadline is not None and now >= job.deadline:
            self.job = job.model_copy(
                update={
                    "state": GuardJobState.CLOSED,
                    "result": GuardJobResult.JOB_TIMEOUT,
                    "deadline": None,
                }
            )

    def apply(self, order: GuardOrder) -> GuardOrderResult:
        """Führt einen Auftrag aus wie source-guard.sh (nur snapshot, prepare und close)."""
        self.advance()
        self.last_seq += 1
        now = self._clock()
        result = self._result(order, now)
        self.last_order = GuardLastOrder(seq=self.last_seq, order=order, result=result, at=now)
        return result

    def _result(self, order: GuardOrder, now: datetime) -> GuardOrderResult:
        job = self.job
        open_job = job is not None and job.open
        match order:
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
                self.job = job.model_copy(
                    update={"state": GuardJobState.PREPARING, "deadline": now + JOB_DEADLINE}
                )
                self._prepare_until = self._monotonic() + self.timings.prepare_seconds
                return GuardOrderResult.PREPARING
            case GuardOrder.CLOSE:
                if job is None or not open_job:
                    return GuardOrderResult.NO_JOB
                self._prepare_until = None
                self.job = job.model_copy(
                    update={
                        "state": GuardJobState.CLOSED,
                        "result": GuardJobResult.CLOSED,
                        "deadline": None,
                    }
                )
                return GuardOrderResult.CLOSED
        # Lauf, Freigabe und Senden simuliert TwinRaid erst mit dem echten Sync (v0.5.2).
        return GuardOrderResult.NOT_SUPPORTED

    def _prepared(self, job: GuardJob, now: datetime) -> tuple[GuardJob, GuardCheck]:
        foreign = self._fault("target-exists")
        clones = 1 if self._fault("target-clone") else 0
        available = 20 * GIB if self._fault("sync-target-refused") else 600 * GIB
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
            size = 350 * MIB
            total += size
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
                        "size": size,
                    }
                )
            )
            mirrors.append(
                GuardCheckMirror(
                    name=item.name,
                    state="mirror",
                    props="ok",
                    count=MIRROR_SNAPSHOTS,
                    newest_created=now - timedelta(days=1),
                    newest_received=now - timedelta(days=1) + timedelta(minutes=12),
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
