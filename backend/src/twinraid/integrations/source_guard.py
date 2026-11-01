"""Status des Wächters auf Klein lesen (Konzept 3.6, „Lesen in TwinRaid“, ab v0.5.0).

Der Worker liest alle 5 Sekunden. Vorher prüft er in /proc/self/mountinfo die beiden
Einbindungen unter /srv/worker/source-guard und mit lstat die Auftragsdatei. status.json
liest er nur als reguläre Datei bis 64 KiB, ohne Symlinks zu folgen, und prüft sie gegen
ein strenges Schema: doppelte Schlüssel, unbekannte Felder und Werte außerhalb der
festen Listen sind ein Fehler. Ob der Wächter aktiv ist, entscheidet erst evaluate()
beim Aufruf von get_status; so stimmt das Alter des Herzschlags auf die Sekunde.

In v0.5.0 schreibt TwinRaid noch keine Aufträge; die Auftragsdatei wird nur geprüft.
"""

import asyncio
import errno
import json
import logging
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import ConfigDict, Field, StringConstraints, ValidationError

from twinraid.shared.base import StrictModel
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
    GuardDatasetProblem,
    GuardDatasetResult,
    GuardDetails,
    GuardError,
    GuardErrorDetail,
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
    GuardSetupProblem,
    GuardState,
    GuardStatusProblem,
    GuardZfs,
    GuardZfsState,
    SourceGuardStatus,
)
from twinraid.shared.status import StatusNotice

logger = logging.getLogger(__name__)

GUARD_DIR = Path("/srv/worker/source-guard")
MOUNTINFO = Path("/proc/self/mountinfo")
READ_INTERVAL_SECONDS = 5.0
# Aktiv heißt: Herzschlag höchstens so alt (Konzept 3.6).
FRESH_SECONDS = 30
MAX_STATUS_BYTES = 64 * 1024
# Protokoll 2 ab source-guard.sh Version 2 (TwinRaid v0.5.1, Konzept 3.6).
PROTOCOL = 2

# Zeichenregeln wie im Wächter.
ContainerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
ShareName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
ContainerId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Instance = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
Compression = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,20}$")]
Guid = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,20}$")]
SnapshotName = Annotated[str, StringConstraints(pattern=r"^twinraid-[0-9]{8}T[0-9]{6}Z$")]
# Uhrzeiten als Sekunden seit 1970; Zahlen höchstens 19 Ziffern wie json_number im Wächter.
Timestamp = Annotated[int, Field(ge=0, lt=10**11)]
Count = Annotated[int, Field(ge=0, lt=10**19)]
Seq = Annotated[int, Field(ge=0, le=999_999_999)]


class _FileModel(StrictModel):
    """Streng: keine Umwandlung von Typen (etwa "1" zu 1 oder true zu 1)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Last(_FileModel):
    seq: Annotated[int, Field(ge=1, le=999_999_999)]
    order: GuardOrder
    container: ContainerName | None
    result: GuardOrderResult
    at: Timestamp


class _Rejected(_FileModel):
    count: Count
    last_at: Timestamp | None


class _Host(_FileModel):
    connect: Literal["present", "absent"]
    flash_backup: Literal["active", "inactive", "unknown"]


class _Limits(_FileModel):
    # Bereiche wie in source-guard.conf (Konzept 3.6).
    max_stop_minutes: Annotated[int, Field(ge=5, le=120)]
    heartbeat_timeout_seconds: Annotated[int, Field(ge=30, le=600)]
    stop_timeout_seconds: Annotated[int, Field(ge=10, le=600)]
    min_run_interval_minutes: Annotated[int, Field(ge=10, le=1440)]
    max_deletions: Annotated[int, Field(ge=0, le=10_000_000)]
    snapshot_keep_days: Annotated[int, Field(ge=1, le=365)]
    snapshot_reserve_percent: Annotated[int, Field(ge=5, le=50)]


class _CheckSummary(_FileModel):
    """Zeile summary von guard.sh sync check (Konzept 3.7)."""

    root: Literal["ok", "missing"]
    conf: Literal["ok", "missing", "invalid"]
    deleg: Literal["ok", "mismatch", "id_in_use"]
    receive: Literal["idle", "busy"]
    clones: Count | None
    available: Count | None
    size: Count | None
    reserve_percent: Annotated[int, Field(ge=0, le=99)] | None
    keep_days: Annotated[int, Field(ge=0, le=9999)] | None
    min_interval_minutes: Annotated[int, Field(ge=0, le=9999)] | None


class _CheckMirror(_FileModel):
    name: ShareName
    state: Literal["absent", "mirror", "foreign"]
    props: Literal["ok", "mismatch"] | None
    count: Count | None
    newest_guid: Guid | None
    newest_created: Timestamp | None
    newest_received: Timestamp | None
    oldest_created: Timestamp | None


class _Check(_FileModel):
    at: Timestamp
    result: GuardCheckResult
    summary: _CheckSummary | None
    mirrors: Annotated[tuple[_CheckMirror, ...], Field(max_length=20)]


class _Target(_FileModel):
    configured: bool
    port: Annotated[int, Field(ge=1, le=65535)]
    key: bool
    known_host: bool
    check: _Check | None


class _Allowed(_FileModel):
    name: ContainerName
    id: ContainerId | None
    state: GuardContainerState


class _Stopped(_FileModel):
    name: ContainerName
    id: ContainerId


class _Run(_FileModel):
    opened_at: Timestamp
    heartbeat_deadline: Timestamp
    max_deadline: Timestamp
    stopped: Annotated[tuple[_Stopped, ...], Field(max_length=50)]


class _Restarted(_FileModel):
    name: ContainerName
    id: ContainerId
    result: GuardRestart


class _LastRun(_FileModel):
    opened_at: Timestamp
    closed_at: Timestamp
    reason: GuardRunEnd
    containers: Annotated[tuple[_Restarted, ...], Field(max_length=50)]


class _Pool(_FileModel):
    used: Count | None
    available: Count | None


class _Base(_FileModel):
    guid: Guid
    created_at: Timestamp


class _Dataset(_FileModel):
    name: ShareName
    present: bool | None
    referenced: Count | None
    compression: Compression | None
    snapshots: Count | None
    oldest_snapshot_at: Timestamp | None
    base: _Base | None
    bookmark: bool | None


class _Zfs(_FileModel):
    state: GuardZfsState
    pool: _Pool | None
    datasets: Annotated[tuple[_Dataset, ...], Field(max_length=20)]
    self_dataset: GuardSelfDataset


class _JobDataset(_FileModel):
    name: ShareName
    mode: Literal["full", "incremental"] | None
    base_guid: Guid | None
    base_type: Literal["snapshot", "bookmark"] | None
    problem: GuardDatasetProblem | None
    deleted: Count | None
    modified: Count | None
    added: Count | None
    renamed: Count | None
    referenced: Count | None
    base_referenced: Count | None
    size: Count | None
    sent: Count | None
    result: GuardDatasetResult | None


class _Job(_FileModel):
    """Job des Wächters (Konzept 3.6, „Job, Prüfen und Senden“)."""

    id: Annotated[int, Field(ge=1, le=999_999_999)]
    state: GuardJobState
    result: GuardJobResult | None
    reason: GuardJobReason | None
    snapshot: SnapshotName
    created_at: Timestamp
    deadline: Timestamp | None
    confirmed: bool
    confirm_reasons: Annotated[tuple[GuardConfirmReason, ...], Field(max_length=3)]
    send_ready: bool | None
    blockers: Annotated[tuple[GuardBlocker, ...], Field(max_length=20)]
    datasets: Annotated[tuple[_JobDataset, ...], Field(max_length=20)]


class GuardFile(_FileModel):
    """state/status.json, wie write_status in source-guard.sh es schreibt."""

    protocol: Literal[2]
    version: Annotated[int, Field(ge=2, le=999)]
    heartbeat_at: Timestamp
    started_at: Timestamp
    instance: Instance | None
    error: GuardError | None
    error_detail: GuardErrorDetail | None
    config_line: Annotated[int, Field(ge=0, le=99_999)] | None
    last_seq: Seq
    last: _Last | None
    rejected: _Rejected
    host: _Host
    limits: _Limits
    target: _Target
    # allow und deny zusammen höchstens 50 (Wächter: too_many).
    allowed: Annotated[tuple[_Allowed, ...], Field(max_length=50)]
    run: _Run | None
    run_allowed_at: Timestamp | None
    last_run: _LastRun | None
    zfs: _Zfs
    job: _Job | None
    seq_exhausted: bool


# --- Einbindungen ------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardPaths:
    base: Path = GUARD_DIR
    mountinfo: Path = MOUNTINFO

    @property
    def request(self) -> Path:
        return self.base / "request"

    @property
    def state(self) -> Path:
        return self.base / "state"

    @property
    def status_file(self) -> Path:
        return self.state / "status.json"


@dataclass(frozen=True)
class Mount:
    root: str
    options: frozenset[str]

    @property
    def read_only(self) -> bool:
        return "ro" in self.options


_OCTAL = re.compile(r"\\([0-7]{3})")
_OWN_ID = re.compile(r"/containers/([0-9a-f]{64})/(?:hostname|hosts|resolv\.conf)$")


def _unescape(field: str) -> str:
    """mountinfo schreibt Leerzeichen, Tab, Zeilenende und Backslash oktal (\\040 …)."""
    return _OCTAL.sub(lambda match: chr(int(match.group(1), 8)), field)


def parse_mountinfo(text: str) -> dict[str, Mount]:
    """Einbindungen je Ziel; bei mehreren gilt die zuletzt eingehängte (die oberste)."""
    mounts: dict[str, Mount] = {}
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 10 or "-" not in fields[6:]:
            continue
        mounts[_unescape(fields[4])] = Mount(
            root=_unescape(fields[3]), options=frozenset(fields[5].split(","))
        )
    return mounts


def own_container_id(text: str) -> str | None:
    """Eigene Container-ID aus den Einbindungen von hostname, hosts und resolv.conf (10.1).

    Nur genau eine ID zählt; fehlt sie oder gibt es mehrere, ist sie unbekannt.
    """
    ids = {
        match.group(1)
        for mount in parse_mountinfo(text).values()
        if (match := _OWN_ID.search(mount.root)) is not None
    }
    return ids.pop() if len(ids) == 1 else None


def check_mounts(paths: GuardPaths, mounts: dict[str, Mount]) -> GuardSetupProblem | None:
    """Fehler in den Einbindungen; None, wenn sie stimmen. Nicht eingebunden prüft der Aufrufer."""
    state = mounts.get(str(paths.state))
    request = mounts.get(str(paths.request))
    if state is None:
        return GuardSetupProblem.STATE_NOT_MOUNTED
    if request is None:
        return GuardSetupProblem.REQUEST_NOT_MOUNTED
    try:
        info = paths.request.lstat()
    except OSError:
        return GuardSetupProblem.REQUEST_NOT_REGULAR
    if stat.S_ISDIR(info.st_mode):
        return GuardSetupProblem.REQUEST_IS_DIRECTORY
    if not stat.S_ISREG(info.st_mode):
        return GuardSetupProblem.REQUEST_NOT_REGULAR
    if not state.read_only:
        return GuardSetupProblem.STATE_WRITABLE
    if request.read_only:
        return GuardSetupProblem.REQUEST_READ_ONLY
    return None


# --- status.json ---------------------------------------------------------------------------


class InvalidStatusError(Exception):
    def __init__(self, problem: GuardStatusProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> object:
    raise ValueError("NaN or Infinity")


def _read_bytes(path: Path) -> bytes | None:
    """Inhalt von status.json; None, wenn die Datei fehlt."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise InvalidStatusError(GuardStatusProblem.NOT_REGULAR) from exc
        raise InvalidStatusError(GuardStatusProblem.UNREADABLE) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InvalidStatusError(GuardStatusProblem.NOT_REGULAR)
        if info.st_size > MAX_STATUS_BYTES:
            raise InvalidStatusError(GuardStatusProblem.TOO_LARGE)
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_STATUS_BYTES:
            chunk = os.read(fd, MAX_STATUS_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        raise InvalidStatusError(GuardStatusProblem.UNREADABLE) from exc
    finally:
        os.close(fd)
    if size > MAX_STATUS_BYTES:
        raise InvalidStatusError(GuardStatusProblem.TOO_LARGE)
    return b"".join(chunks)


def parse_status(raw: bytes) -> GuardFile:
    """Prüft status.json; wirft InvalidStatusError mit dem Grund."""
    try:
        text = raw.decode("ascii")
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidStatusError(GuardStatusProblem.BAD_JSON) from exc
    if not isinstance(data, dict):
        raise InvalidStatusError(GuardStatusProblem.SCHEMA)
    protocol = data.get("protocol")
    # bool ist in Python ein int; true zählt nicht als Protokoll 1.
    if type(protocol) is int and protocol != PROTOCOL:
        raise InvalidStatusError(GuardStatusProblem.PROTOCOL_MISMATCH)
    try:
        return GuardFile.model_validate_json(raw)
    except ValidationError as exc:
        raise InvalidStatusError(GuardStatusProblem.SCHEMA) from exc


# --- Beobachtung und Auswertung ------------------------------------------------------------


@dataclass(frozen=True)
class GuardObservation:
    """Ergebnis eines Lesevorgangs; ausgewertet wird erst mit der aktuellen Uhrzeit."""

    mounted: bool
    setup_problem: GuardSetupProblem | None = None
    file: GuardFile | None = None
    status_problem: GuardStatusProblem | None = None
    # Letzter bekannter Herzschlag, auch aus einem früheren Lesevorgang.
    last_heartbeat: int | None = None
    own_id: str | None = None


NOT_READ = GuardObservation(mounted=True)


def read_observation(paths: GuardPaths, last_heartbeat: int | None = None) -> GuardObservation:
    try:
        mountinfo = paths.mountinfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        mountinfo = ""
    mounts = parse_mountinfo(mountinfo)
    own_id = own_container_id(mountinfo)
    if str(paths.state) not in mounts and str(paths.request) not in mounts:
        return GuardObservation(mounted=False, own_id=own_id)
    setup_problem = check_mounts(paths, mounts)
    try:
        raw = _read_bytes(paths.status_file)
        file = parse_status(raw) if raw is not None else None
    except InvalidStatusError as exc:
        return GuardObservation(
            mounted=True,
            setup_problem=setup_problem,
            status_problem=exc.problem,
            last_heartbeat=last_heartbeat,
            own_id=own_id,
        )
    if file is not None:
        last_heartbeat = file.heartbeat_at
    return GuardObservation(
        mounted=True,
        setup_problem=setup_problem,
        file=file,
        last_heartbeat=last_heartbeat,
        own_id=own_id,
    )


def _time(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _details(file: GuardFile) -> GuardDetails:
    pool = file.zfs.pool
    return GuardDetails(
        version=file.version,
        started_at=_time(file.started_at),
        allowed=tuple(GuardContainer(name=item.name, state=item.state) for item in file.allowed),
        run=(
            GuardRun(
                opened_at=_time(file.run.opened_at),
                heartbeat_deadline=_time(file.run.heartbeat_deadline),
                max_deadline=_time(file.run.max_deadline),
                stopped=tuple(item.name for item in file.run.stopped),
            )
            if file.run is not None
            else None
        ),
        last_run=(
            GuardLastRun(
                opened_at=_time(file.last_run.opened_at),
                closed_at=_time(file.last_run.closed_at),
                reason=file.last_run.reason,
                containers=tuple(
                    GuardRestartResult(name=item.name, result=item.result)
                    for item in file.last_run.containers
                ),
            )
            if file.last_run is not None
            else None
        ),
        run_allowed_at=_time(file.run_allowed_at) if file.run_allowed_at is not None else None,
        zfs=GuardZfs(
            state=file.zfs.state,
            pool_used=pool.used if pool is not None else None,
            pool_available=pool.available if pool is not None else None,
            datasets=tuple(
                GuardDataset(
                    name=item.name,
                    present=item.present,
                    referenced=item.referenced,
                    compression=item.compression,
                    snapshots=item.snapshots,
                    oldest_snapshot_at=_maybe_time(item.oldest_snapshot_at),
                    base_created_at=(
                        _time(item.base.created_at) if item.base is not None else None
                    ),
                    bookmark=item.bookmark,
                )
                for item in file.zfs.datasets
            ),
            self_dataset=file.zfs.self_dataset,
        ),
        limits=GuardLimits(**file.limits.model_dump()),
        target_configured=file.target.configured,
        target_port=file.target.port,
        rejected_count=file.rejected.count,
        instance=file.instance,
        last_seq=file.last_seq,
        last_order=(
            GuardLastOrder(
                seq=file.last.seq,
                order=file.last.order,
                result=file.last.result,
                at=_time(file.last.at),
            )
            if file.last is not None
            else None
        ),
        target_key=file.target.key,
        target_known_host=file.target.known_host,
        target_check=_check(file.target.check) if file.target.check is not None else None,
        job=_job(file.job) if file.job is not None else None,
    )


def _maybe_time(seconds: int | None) -> datetime | None:
    return _time(seconds) if seconds is not None else None


def _check(check: _Check) -> GuardCheck:
    summary = check.summary
    return GuardCheck(
        at=_time(check.at),
        result=check.result,
        summary=(GuardCheckSummary(**summary.model_dump()) if summary is not None else None),
        mirrors=tuple(
            GuardCheckMirror(
                name=item.name,
                state=item.state,
                props=item.props,
                count=item.count,
                newest_created=_maybe_time(item.newest_created),
                newest_received=_maybe_time(item.newest_received),
                oldest_created=_maybe_time(item.oldest_created),
            )
            for item in check.mirrors
        ),
    )


def _job(job: _Job) -> GuardJob:
    return GuardJob(
        id=job.id,
        state=job.state,
        result=job.result,
        reason=job.reason,
        snapshot=job.snapshot,
        created_at=_time(job.created_at),
        deadline=_maybe_time(job.deadline),
        confirmed=job.confirmed,
        confirm_reasons=job.confirm_reasons,
        send_ready=job.send_ready,
        blockers=job.blockers,
        datasets=tuple(
            GuardJobDataset(
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
                sent=item.sent,
                result=item.result,
            )
            for item in job.datasets
        ),
    )


def _notices(file: GuardFile, own_id: str | None) -> tuple[StatusNotice, ...]:
    notices: list[StatusNotice] = []
    if file.host.connect == "present":
        notices.append(StatusNotice.SOURCE_CONNECT_PLUGIN)
    if file.host.flash_backup == "active":
        notices.append(StatusNotice.SOURCE_FLASH_BACKUP)
    if own_id is not None and any(item.id == own_id for item in file.allowed):
        notices.append(StatusNotice.SOURCE_GUARD_ALLOWS_SELF)
    return tuple(notices)


@dataclass(frozen=True)
class GuardReport:
    status: SourceGuardStatus
    # Hinweise für die Kachel Klein; nur aus einem frischen Status.
    notices: tuple[StatusNotice, ...] = ()


def evaluate(observation: GuardObservation, now: datetime) -> GuardReport:
    if not observation.mounted:
        return GuardReport(SourceGuardStatus(state=GuardState.NOT_MOUNTED))
    last = observation.last_heartbeat
    last_at = _time(last) if last is not None else None
    if observation.status_problem is not None:
        return GuardReport(
            SourceGuardStatus(
                state=GuardState.INVALID_STATUS,
                status_problem=observation.status_problem,
                setup_problem=observation.setup_problem,
                heartbeat_at=last_at,
            )
        )
    file = observation.file
    if file is None:
        state = GuardState.MISCONFIGURED if observation.setup_problem else GuardState.INACTIVE
        return GuardReport(
            SourceGuardStatus(
                state=state, setup_problem=observation.setup_problem, heartbeat_at=last_at
            )
        )
    heartbeat = _time(file.heartbeat_at)
    age = (now - heartbeat) // timedelta(seconds=1)
    if age < -FRESH_SECONDS:
        return GuardReport(
            SourceGuardStatus(
                state=GuardState.INVALID_STATUS,
                status_problem=GuardStatusProblem.HEARTBEAT_FUTURE,
                setup_problem=observation.setup_problem,
                heartbeat_at=heartbeat,
            )
        )
    if age > FRESH_SECONDS:
        state = GuardState.MISCONFIGURED if observation.setup_problem else GuardState.INACTIVE
        return GuardReport(
            SourceGuardStatus(
                state=state, setup_problem=observation.setup_problem, heartbeat_at=heartbeat
            )
        )
    if observation.setup_problem is not None:
        state = GuardState.MISCONFIGURED
    elif file.error is not None:
        state = GuardState.ERROR
    else:
        state = GuardState.ACTIVE
    return GuardReport(
        SourceGuardStatus(
            state=state,
            setup_problem=observation.setup_problem,
            error=file.error,
            error_detail=file.error_detail,
            config_line=file.config_line if file.error is GuardError.CONFIG_INVALID else None,
            heartbeat_at=heartbeat,
            heartbeat_age_seconds=max(age, 0),
            details=_details(file),
        ),
        _notices(file, observation.own_id),
    )


# --- Quelle für den Worker ------------------------------------------------------------------


class SourceGuardSource(Protocol):
    def report(self) -> GuardReport: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _summary(status: SourceGuardStatus) -> str:
    """Nur feste Codes, nie Namen (Konzept 3.6)."""
    parts = [status.state.value]
    for code in (status.setup_problem, status.status_problem, status.error, status.error_detail):
        if code is not None:
            parts.append(code.value)
    return " ".join(parts)


class RealSourceGuard:
    """Liest den Status alle 5 Sekunden; get_status wertet den letzten Stand aus."""

    def __init__(
        self,
        paths: GuardPaths | None = None,
        interval: float = READ_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._paths = paths or GuardPaths()
        self._interval = interval
        self._clock = clock
        self._observation = NOT_READ
        self._logged: str | None = None

    def report(self) -> GuardReport:
        return evaluate(self._observation, self._clock())

    async def refresh(self) -> None:
        try:
            self._observation = await asyncio.to_thread(
                read_observation, self._paths, self._observation.last_heartbeat
            )
        except Exception:  # Ein Fehler beim Lesen darf den Worker nie beenden.
            logger.exception("reading the source guard status failed")
            return
        summary = _summary(self.report().status)
        if summary != self._logged:
            logger.info("source guard: %s", summary)
            self._logged = summary

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self._interval)
