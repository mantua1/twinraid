"""Simulierter Wächter auf Klein (Konzept 6.4, ab v0.5.0).

Standard: aktiv, ein freigegebener Container twinraid-testcontainer, Dataset twinraidtest
mit Kompression on. Fehler mit twinraid-admin mock-fault:

- source-guard-inactive: kein Herzschlag mehr seit dem Einschalten des Fehlers;
- source-guard-lost: der Wächter hat seinen Zustand verloren (state_lost);
- source-guard-start-failed: ein angehaltener Container startete am Ende des Laufs nicht
  wieder; danach läuft die Pause bis zum nächsten Lauf.

Mit mock-tour zeigt GUARD_SCENARIO jeden Zustand, jeden Fehler und jeden Hinweis des
Wächters mindestens einmal, im selben Takt wie der Rundlauf von Groß und VM.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from twinraid.integrations.mocks.lab import fault_flag, tour_flag
from twinraid.integrations.mocks.status import STEP_SECONDS
from twinraid.integrations.source_guard import GuardReport
from twinraid.shared.source_guard import (
    GuardContainer,
    GuardContainerState,
    GuardDataset,
    GuardDetails,
    GuardError,
    GuardErrorDetail,
    GuardLastRun,
    GuardLimits,
    GuardRestart,
    GuardRestartResult,
    GuardRun,
    GuardRunEnd,
    GuardSelfDataset,
    GuardSetupProblem,
    GuardState,
    GuardStatusProblem,
    GuardZfs,
    GuardZfsState,
    SourceGuardStatus,
)
from twinraid.shared.status import StatusNotice

MOCK_CONTAINER = "twinraid-testcontainer"
MOCK_DATASET = "twinraidtest"
# Weitere Namen nur für den Rundlauf.
_TOUR_CONTAINERS = ("twinraid-testcontainer-2", "twinraid-testcontainer-3")
GIB = 1024**3
# Die Pause nach einem Lauf wie im Standard von source-guard.conf.
RUN_INTERVAL = timedelta(minutes=30)
# Dauer eines simulierten Laufs vor dem gescheiterten Neustart.
RUN_DURATION = timedelta(minutes=3)
# Der Wächter schreibt seinen Status alle 5 Sekunden.
WRITE_SECONDS = 5

_LIMITS = GuardLimits(
    max_stop_minutes=20,
    heartbeat_timeout_seconds=120,
    stop_timeout_seconds=60,
    min_run_interval_minutes=30,
    max_deletions=1000,
    snapshot_keep_days=7,
    snapshot_reserve_percent=15,
)
_ZFS = GuardZfs(
    state=GuardZfsState.OK,
    pool_used=120 * GIB,
    pool_available=680 * GIB,
    datasets=(GuardDataset(name=MOCK_DATASET, present=True, referenced=2 * GIB, compression="on"),),
    self_dataset=GuardSelfDataset.FOUND,
)


def _details(started_at: datetime, **changes: object) -> GuardDetails:
    details = GuardDetails(
        version=1,
        started_at=started_at,
        allowed=(GuardContainer(name=MOCK_CONTAINER, state=GuardContainerState.RUNNING),),
        run=None,
        last_run=None,
        run_allowed_at=None,
        zfs=_ZFS,
        limits=_LIMITS,
        target_configured=False,
        target_port=22,
        rejected_count=0,
    )
    return details.model_copy(update=changes)


def _heartbeat(now: datetime) -> tuple[datetime, int]:
    """Letzter Schreibzeitpunkt im 5-Sekunden-Takt und sein Alter."""
    seconds = int(now.timestamp())
    written = seconds - seconds % WRITE_SECONDS
    return datetime.fromtimestamp(written, UTC), seconds - written


def _active(now: datetime, started_at: datetime, **changes: object) -> SourceGuardStatus:
    heartbeat, age = _heartbeat(now)
    return SourceGuardStatus(
        state=GuardState.ACTIVE,
        heartbeat_at=heartbeat,
        heartbeat_age_seconds=age,
        details=_details(started_at, **changes),
    )


def _error(
    now: datetime,
    started_at: datetime,
    error: GuardError,
    detail: GuardErrorDetail | None = None,
    line: int | None = None,
    **changes: object,
) -> SourceGuardStatus:
    status = _active(now, started_at, **changes)
    return status.model_copy(
        update={
            "state": GuardState.ERROR,
            "error": error,
            "error_detail": detail,
            "config_line": line,
        }
    )


def _start_failed_run(closed_at: datetime) -> GuardLastRun:
    return GuardLastRun(
        opened_at=closed_at - RUN_DURATION,
        closed_at=closed_at,
        reason=GuardRunEnd.FINISH,
        containers=(GuardRestartResult(name=MOCK_CONTAINER, result=GuardRestart.START_FAILED),),
    )


@dataclass(frozen=True)
class GuardStep:
    """Ein Schritt des Rundlaufs; t0 ist der Beginn des Schritts."""

    build: Callable[[datetime, datetime, datetime], SourceGuardStatus]
    notices: tuple[StatusNotice, ...] = ()


def _step(
    build: Callable[[datetime, datetime, datetime], SourceGuardStatus],
    *notices: StatusNotice,
) -> GuardStep:
    return GuardStep(build, notices)


def _setup(problem: GuardSetupProblem) -> GuardStep:
    def build(now: datetime, started: datetime, _t0: datetime) -> SourceGuardStatus:
        status = _active(now, started)
        return status.model_copy(
            update={"state": GuardState.MISCONFIGURED, "setup_problem": problem}
        )

    return GuardStep(build)


def _invalid(problem: GuardStatusProblem) -> GuardStep:
    def build(_now: datetime, _started: datetime, t0: datetime) -> SourceGuardStatus:
        return SourceGuardStatus(
            state=GuardState.INVALID_STATUS,
            status_problem=problem,
            heartbeat_at=t0 - timedelta(seconds=40),
        )

    return GuardStep(build)


def _tour_run(now: datetime, started: datetime, t0: datetime) -> SourceGuardStatus:
    return _active(
        now,
        started,
        allowed=(GuardContainer(name=MOCK_CONTAINER, state=GuardContainerState.STOPPED),),
        run=GuardRun(
            opened_at=t0 - timedelta(minutes=1),
            heartbeat_deadline=t0 + timedelta(minutes=1),
            max_deadline=t0 + timedelta(minutes=19),
            stopped=(MOCK_CONTAINER,),
        ),
    )


def _tour_last_run(
    reason: GuardRunEnd, *results: GuardRestart
) -> Callable[[datetime, datetime, datetime], SourceGuardStatus]:
    names = (MOCK_CONTAINER, *_TOUR_CONTAINERS)

    def build(now: datetime, started: datetime, t0: datetime) -> SourceGuardStatus:
        closed = t0 - timedelta(minutes=2)
        return _active(
            now,
            started,
            allowed=tuple(
                GuardContainer(
                    name=name,
                    state=(
                        GuardContainerState.RUNNING
                        if result in (GuardRestart.STARTED, GuardRestart.ALREADY_RUNNING)
                        else GuardContainerState.STOPPED
                    ),
                )
                for name, result in zip(names, results, strict=False)
            ),
            last_run=GuardLastRun(
                opened_at=closed - RUN_DURATION,
                closed_at=closed,
                reason=reason,
                containers=tuple(
                    GuardRestartResult(name=name, result=result)
                    for name, result in zip(names, results, strict=False)
                ),
            ),
            run_allowed_at=closed + RUN_INTERVAL,
        )

    return build


def _zfs(**changes: object) -> Callable[[datetime, datetime, datetime], SourceGuardStatus]:
    def build(now: datetime, started: datetime, _t0: datetime) -> SourceGuardStatus:
        return _active(now, started, zfs=_ZFS.model_copy(update=changes))

    return build


_APPDATA = GuardDataset(name="appdata", present=True, referenced=4 * GIB, compression="lz4")
_MISSING = GuardDataset(name="isos", present=False, referenced=None, compression=None)
_OFF = GuardDataset(name=MOCK_DATASET, present=True, referenced=2 * GIB, compression="off")


def _not_mounted(_now: datetime, _started: datetime, _t0: datetime) -> SourceGuardStatus:
    return SourceGuardStatus(state=GuardState.NOT_MOUNTED)


def _inactive_since(_now: datetime, _started: datetime, t0: datetime) -> SourceGuardStatus:
    return SourceGuardStatus(state=GuardState.INACTIVE, heartbeat_at=t0 - timedelta(minutes=2))


def _never_seen(_now: datetime, _started: datetime, _t0: datetime) -> SourceGuardStatus:
    return SourceGuardStatus(state=GuardState.INACTIVE)


def _guard_error(
    error: GuardError, detail: GuardErrorDetail | None = None, line: int | None = None
) -> Callable[[datetime, datetime, datetime], SourceGuardStatus]:
    def build(now: datetime, started: datetime, _t0: datetime) -> SourceGuardStatus:
        return _error(now, started, error, detail, line)

    return build


def _docker_unavailable(now: datetime, started: datetime, _t0: datetime) -> SourceGuardStatus:
    return _error(
        now,
        started,
        GuardError.DOCKER_UNAVAILABLE,
        allowed=(GuardContainer(name=MOCK_CONTAINER, state=GuardContainerState.UNKNOWN),),
    )


def _allows_self(now: datetime, started: datetime, _t0: datetime) -> SourceGuardStatus:
    return _error(
        now,
        started,
        GuardError.CONFIG_INVALID,
        GuardErrorDetail.ALLOW_CONTAINS_TWINRAID,
        allowed=(
            GuardContainer(name=MOCK_CONTAINER, state=GuardContainerState.RUNNING),
            GuardContainer(name="twinraid", state=GuardContainerState.SELF),
        ),
    )


def _missing_container(now: datetime, started: datetime, _t0: datetime) -> SourceGuardStatus:
    return _active(
        now,
        started,
        allowed=(GuardContainer(name=MOCK_CONTAINER, state=GuardContainerState.MISSING),),
    )


def _plain(now: datetime, started: datetime, _t0: datetime) -> SourceGuardStatus:
    return _active(now, started)


# Rundlauf des Wächters (Konzept 6.4); ein Test prüft die Vollständigkeit.
GUARD_SCENARIO: tuple[GuardStep, ...] = (
    _step(_plain),
    _step(_not_mounted),
    _step(_inactive_since),
    _step(_never_seen),
    _step(_guard_error(GuardError.CONFIG_INVALID, GuardErrorDetail.BAD_VALUE, 7)),
    _step(_guard_error(GuardError.MISCONFIGURED, GuardErrorDetail.MOUNT_FAILED)),
    _step(_guard_error(GuardError.REQUEST_NOT_A_FILE, GuardErrorDetail.REQUEST_IS_DIRECTORY)),
    _step(_guard_error(GuardError.STATE_LOST)),
    _step(_docker_unavailable),
    *(_setup(problem) for problem in GuardSetupProblem),
    *(_invalid(problem) for problem in GuardStatusProblem),
    _step(_plain, StatusNotice.SOURCE_CONNECT_PLUGIN, StatusNotice.SOURCE_FLASH_BACKUP),
    _step(_allows_self, StatusNotice.SOURCE_GUARD_ALLOWS_SELF),
    _step(_missing_container),
    _step(_tour_run),
    _step(_tour_last_run(GuardRunEnd.FINISH, GuardRestart.STARTED, GuardRestart.ALREADY_RUNNING)),
    _step(_tour_last_run(GuardRunEnd.DEADMAN_HEARTBEAT, GuardRestart.START_FAILED)),
    _step(_tour_last_run(GuardRunEnd.DEADMAN_MAX, GuardRestart.CHANGED)),
    _step(_tour_last_run(GuardRunEnd.STOP_COMMAND, GuardRestart.GONE)),
    _step(
        _zfs(
            state=GuardZfsState.UNAVAILABLE,
            pool_used=None,
            pool_available=None,
            datasets=(
                GuardDataset(name=MOCK_DATASET, present=None, referenced=None, compression=None),
            ),
            self_dataset=GuardSelfDataset.UNKNOWN,
        )
    ),
    _step(_zfs(datasets=(_APPDATA, _MISSING, _OFF), self_dataset=GuardSelfDataset.MISSING)),
    _step(_zfs(datasets=(_APPDATA,), self_dataset=GuardSelfDataset.OUTSIDE)),
    _step(_zfs(datasets=(_APPDATA,), self_dataset=GuardSelfDataset.NO_CONTAINER)),
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MockSourceGuard:
    def __init__(
        self,
        flag_dir: Path,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._flag_dir = flag_dir
        self._monotonic = monotonic
        self._clock = clock
        self._started = monotonic()
        self._started_at = clock().replace(microsecond=0)

    def _fault_since(self, name: str) -> datetime | None:
        """Zeitpunkt, an dem der Fehler eingeschaltet wurde; None, wenn er aus ist."""
        try:
            mtime = fault_flag(self._flag_dir, name).stat().st_mtime
        except FileNotFoundError:
            return None
        return datetime.fromtimestamp(int(mtime), UTC)

    def tour_step(self) -> int:
        return int((self._monotonic() - self._started) // STEP_SECONDS) % len(GUARD_SCENARIO)

    def report(self) -> GuardReport:
        now = self._clock()
        if tour_flag(self._flag_dir).exists():
            steps = int((self._monotonic() - self._started) // STEP_SECONDS)
            t0 = self._started_at + timedelta(seconds=steps * STEP_SECONDS)
            step = GUARD_SCENARIO[steps % len(GUARD_SCENARIO)]
            return GuardReport(step.build(now, self._started_at, t0), step.notices)
        inactive = self._fault_since("source-guard-inactive")
        if inactive is not None:
            return GuardReport(SourceGuardStatus(state=GuardState.INACTIVE, heartbeat_at=inactive))
        changes: dict[str, object] = {}
        failed = self._fault_since("source-guard-start-failed")
        if failed is not None:
            allowed_at = failed + RUN_INTERVAL
            changes = {
                "allowed": (
                    GuardContainer(name=MOCK_CONTAINER, state=GuardContainerState.STOPPED),
                ),
                "last_run": _start_failed_run(failed),
                "run_allowed_at": allowed_at if allowed_at > now else None,
            }
        status = _active(now, self._started_at, **changes)
        if self._fault_since("source-guard-lost") is not None:
            status = status.model_copy(
                update={"state": GuardState.ERROR, "error": GuardError.STATE_LOST}
            )
        return GuardReport(status)
