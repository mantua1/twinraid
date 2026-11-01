"""Sitzung vm-watch: VM-Status von Unraid Groß über guard.sh (Konzept 3.5, ab v0.4.3).

Solange Groß online ist, hält der Worker eine SSH-Verbindung mit dem Key „status“
offen. guard.sh fragt lokal alle 3 s ab und schickt eine Zeile bei jeder Änderung,
sonst alle 15 s. Status und Abläufe lesen nur den letzten Stand dieser Sitzung; so
gibt es eine Anmeldung je Start von Groß statt einer je Abfrage.

Die Zeile enthält nur feste Werte und hex-kodierte VM-Namen. Die Namen werden
bereinigt und nie geloggt; ins Container-Log kommen nur feste Codes.

Nach einem Fehler wartet der Worker; der Status nennt den nächsten Versuch. Ein
erfolgreiches twinraid-admin trust-host-key beendet jede Wartezeit (ab v0.4.4).
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from twinraid.integrations.ssh import (
    REQUIRED_GUARD_VERSION,
    SSH_BINARY,
    SSH_ENVIRONMENT,
    SshTarget,
    classify,
    ssh_arguments,
)
from twinraid.integrations.vm_list import display_names
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import StatusReason, WatchRetry

logger = logging.getLogger(__name__)

WATCH_REQUEST = "vm-watch"
MAX_LINE_BYTES = 16 * 1024
_MAX_STDERR_BYTES = 64 * 1024
# Zustände der VM aus power.conf: libvirt wie bei der Unraid-API, dazu die von guard.sh.
LIBVIRT_STATES = frozenset(
    {"RUNNING", "IDLE", "PAUSED", "SHUTDOWN", "SHUTOFF", "CRASHED", "PMSUSPENDED", "NOSTATE"}
)
GUARD_STATES = frozenset({"NOT_CONFIGURED", "NOT_FOUND", "LIBVIRT_DOWN", "UNKNOWN"})
_HEX = r"(?:[0-9a-f]{2}){1,256}"
_LINE = re.compile(
    rf"watch (?P<version>[0-9]{{1,4}}) vm=(?P<vm>[A-Z_]{{1,32}}) name=(?P<name>-|{_HEX}) "
    rf"others=(?P<others>\?|[0-9]{{1,4}}) names=(?P<names>-|{_HEX}(?:,{_HEX}){{0,19}}) "
    r"connect=(?P<connect>present|absent) flash_backup=(?P<flash>active|inactive|unknown) "
    r"arc_max=(?P<arc_max>-|\?|[0-9]{1,20}) c_max=(?P<c_max>-|\?|[0-9]{1,20})",
    re.ASCII,
)
_VERSION = re.compile(r"watch ([0-9]{1,4}) ", re.ASCII)
MAX_NAMES = 20
# Diese Fehler behebt kein erneuter Versuch; der Worker wartet (Konzept 3.5, Neuversuche).
PERMANENT_REASONS = frozenset(
    {
        StatusReason.SSH_KEY_MISSING,
        StatusReason.SSH_HOST_UNTRUSTED,
        StatusReason.SSH_HOST_KEY_MISMATCH,
        StatusReason.SSH_AUTH_FAILED,
        StatusReason.SSH_DENIED,
        StatusReason.GUARD_OUTDATED,
    }
)
# So scheitert eine Anmeldung, solange sshd auf einem gerade gestarteten Groß noch fehlt.
_BOOTING = frozenset({StatusReason.SSH_UNREACHABLE, StatusReason.SSH_FAILED})


class WatchLineError(ValueError):
    """Die Zeile von guard.sh hat nicht das erwartete Format."""

    def __init__(self, outdated: bool = False) -> None:
        super().__init__("guard.sh is outdated" if outdated else "unexpected line from vm-watch")
        self.outdated = outdated


@dataclass(frozen=True)
class WatchReport:
    """Der Stand aus einer Zeile von vm-watch."""

    # Zustand der VM aus power.conf (LIBVIRT_STATES oder GUARD_STATES).
    vm: str
    # Name der VM aus power.conf, falls guard.sh ihn kennt.
    name: str | None
    # Weitere laufende VMs, bereinigt; None, wenn die Liste nicht lesbar war.
    others: tuple[str, ...] | None
    connect_plugin: bool
    # active, inactive oder unknown
    flash_backup: str
    # Grenze des ZFS-Caches auf Groß in Byte (ab Version 3): zfs_arc_max aus Unraid (0: keine
    # eingetragen) und die wirksame Obergrenze c_max; None, wenn ZFS nicht geladen oder der
    # Wert nicht lesbar ist (Konzept 3.5, "Grenze des ARC").
    arc_max: int | None
    c_max: int | None


def _decode(hex_text: str) -> str:
    return bytes.fromhex(hex_text).decode("utf-8", errors="replace")


def _bytes(value: str) -> int | None:
    """Byte aus der Statuszeile; "-" (ZFS nicht geladen) und "?" (nicht lesbar) sind None."""
    return int(value) if value.isdigit() else None


def parse_line(raw: bytes) -> WatchReport:
    """Prüft eine Zeile streng. Wirft WatchLineError."""
    try:
        text = raw.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        raise WatchLineError from None
    match = _LINE.fullmatch(text)
    if match is None:
        version = _VERSION.match(text)
        raise WatchLineError(
            outdated=version is not None and int(version.group(1)) < REQUIRED_GUARD_VERSION
        )
    if int(match["version"]) < REQUIRED_GUARD_VERSION:
        raise WatchLineError(outdated=True)
    vm = match["vm"]
    if vm not in LIBVIRT_STATES | GUARD_STATES:
        raise WatchLineError
    names = [] if match["names"] == "-" else match["names"].split(",")
    others: tuple[str, ...] | None
    if match["others"] == "?":
        if names:
            raise WatchLineError
        others = None
    else:
        count = int(match["others"])
        if len(names) != min(count, MAX_NAMES):
            raise WatchLineError
        others = display_names(_decode(name) for name in names)
    return WatchReport(
        vm=vm,
        name=None if match["name"] == "-" else _decode(match["name"]),
        others=others,
        connect_plugin=match["connect"] == "present",
        flash_backup=match["flash"],
        arc_max=_bytes(match["arc_max"]),
        c_max=_bytes(match["c_max"]),
    )


@dataclass(frozen=True)
class WatchView:
    """Was der Worker gerade über die VM weiß: ein Stand oder der Grund, warum nicht."""

    report: WatchReport | None
    reason: StatusReason | None = None
    # Der Worker wartet nach einem Fehler; wann der nächste Versuch kommt (ab v0.4.4).
    retry: WatchRetry | None = None


class WatchState(Protocol):
    """Was Status und Abläufe von der Sitzung brauchen (in Tests ersetzbar)."""

    def view(self) -> WatchView: ...

    def set_target_online(self, online: bool) -> None: ...


def _reason_of(outcome: ActionOutcome) -> StatusReason:
    """Ursache einer gescheiterten Sitzung; Unbekanntes zählt als Unterbrechung."""
    try:
        return StatusReason(outcome.value)
    except ValueError:
        return StatusReason.WATCH_INTERRUPTED


def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def watch_outcome(reason: StatusReason | None) -> ActionOutcome:
    """Ursache für Abläufe, wenn die Sitzung gerade keinen Stand hat."""
    if reason is not None:
        with contextlib.suppress(ValueError):
            return ActionOutcome(reason.value)
    return ActionOutcome.WATCH_INTERRUPTED


@dataclass(frozen=True)
class WatchTimings:
    """Feste Zeiten der Sitzung (Konzept 3.5)."""

    # Ohne Zeile so lange gilt die Sitzung als hängend.
    stall_seconds: float = 40.0
    # Wartezeiten nach einem Abbruch; die letzte gilt für alle weiteren Versuche.
    retry_delays: tuple[float, ...] = (5.0, 15.0, 60.0, 300.0)
    # Nach einer so langen Sitzung beginnt die Folge der Wartezeiten von vorn.
    stable_session_seconds: float = 60.0
    # Pause nach einem dauerhaften Fehler, falls sich nichts ändert.
    permanent_pause_seconds: float = 1800.0
    # Abstand der Versuche, solange sshd auf einem gerade gestarteten Groß noch fehlt.
    boot_retry_seconds: float = 3.0
    # Während einer Pause: so oft prüfen, ob sich Key oder Hostkey geändert haben.
    file_poll_seconds: float = 5.0


FileSignature = tuple[tuple[int, int, int] | None, ...]


class RetryPlan:
    """Wann darf die nächste Anmeldung stattfinden? (Konzept 3.5, Neuversuche)."""

    def __init__(self, timings: WatchTimings, boot_window_seconds: float) -> None:
        self._timings = timings
        self._boot_window = boot_window_seconds
        self._failures = 0
        self._not_before = 0.0
        # Signatur der Key-Dateien bei einem dauerhaften Fehler; None: keine Pause.
        self._paused_at_files: FileSignature | None = None
        self._boot_until: float | None = None

    def _reset(self) -> None:
        self._failures = 0
        self._not_before = 0.0
        self._paused_at_files = None

    def retry_now(self) -> None:
        """Jede Wartezeit endet sofort (trust-host-key mit Erfolg, ab v0.4.4)."""
        self._reset()

    def came_online(self, now: float) -> None:
        """Groß war offline und ist jetzt erreichbar: neuer Anfang mit Startfenster."""
        self._reset()
        self._boot_until = now + self._boot_window

    def went_offline(self) -> None:
        self._reset()
        self._boot_until = None

    def in_boot_window(self, now: float) -> bool:
        return self._boot_until is not None and now < self._boot_until

    @property
    def paused(self) -> bool:
        return self._paused_at_files is not None

    def transient(self, now: float, session_seconds: float, booting: bool) -> None:
        """Abbruch oder gescheiterte Anmeldung, die ein neuer Versuch beheben kann."""
        if session_seconds >= self._timings.stable_session_seconds:
            self._failures = 0
        if booting and self.in_boot_window(now):
            self._not_before = now + self._timings.boot_retry_seconds
            return
        delays = self._timings.retry_delays
        self._not_before = now + delays[min(self._failures, len(delays) - 1)]
        self._failures += 1

    def permanent(self, now: float, files: FileSignature) -> None:
        """Dauerhafter Fehler: erst wieder nach einer Änderung oder nach der Pause."""
        self._paused_at_files = files
        self._not_before = now + self._timings.permanent_pause_seconds

    def may_connect(self, now: float, files: FileSignature) -> bool:
        if self._paused_at_files is not None and files != self._paused_at_files:
            self._reset()
        return now >= self._not_before

    def remaining(self, now: float) -> float:
        """Sekunden bis zum nächsten erlaubten Versuch (ohne Prüfung der Dateien)."""
        return max(0.0, self._not_before - now)

    def wait_seconds(self, now: float) -> float:
        remaining = self.remaining(now)
        if self._paused_at_files is not None:
            return min(remaining, self._timings.file_poll_seconds)
        return remaining


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _rounded_up(moment: datetime) -> datetime:
    """Auf volle Sekunden aufgerundet: Die Anzeige nennt nie eine zu frühe Zeit."""
    whole = moment.replace(microsecond=0)
    return whole if whole == moment else whole + timedelta(seconds=1)


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns)


async def _read_limited(stream: asyncio.StreamReader | None) -> str:
    """Liest stderr vollständig, behält aber höchstens 64 KiB (sonst blockiert ssh)."""
    if stream is None:
        return ""
    kept = b""
    while chunk := await stream.read(4096):
        if len(kept) < _MAX_STDERR_BYTES:
            kept += chunk[: _MAX_STDERR_BYTES - len(kept)]
    return kept.decode(errors="replace")


class VmWatch:
    """Hält die Sitzung vm-watch, solange Groß online ist."""

    def __init__(
        self,
        target: SshTarget,
        boot_window_seconds: float,
        timings: WatchTimings | None = None,
        binary: Path = SSH_BINARY,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._target = target
        self._timings = timings or WatchTimings()
        self._binary = binary
        self._monotonic = monotonic
        self._clock = clock
        self._plan = RetryPlan(self._timings, boot_window_seconds)
        self._online: bool | None = None
        # Zählt jeden Wechsel nach offline; eine ältere Sitzung meldet dann nichts mehr.
        self._generation = 0
        self._view = WatchView(None, StatusReason.WATCH_CONNECTING)
        self._process: asyncio.subprocess.Process | None = None
        self._wakeup = asyncio.Event()
        # Gesetzt bei jeder Änderung des Stands; der Monitor prüft dann sofort neu.
        self.changed = asyncio.Event()
        self._logged: dict[str, float] = {}

    def view(self) -> WatchView:
        return self._view

    def set_target_online(self, online: bool) -> None:
        """Ergebnis der TCP-Prüfung: Die Sitzung läuft nur, solange Groß online ist."""
        previous, self._online = self._online, online
        if online == previous:
            return
        if online:
            if previous is False:
                self._plan.came_online(self._monotonic())
        else:
            self._generation += 1
            self._plan.went_offline()
            self._set(WatchView(None, StatusReason.WATCH_CONNECTING))
            self._kill()
        self._wakeup.set()

    def retry_now(self) -> None:
        """Beendet jede Wartezeit und versucht es sofort (Konzept 3.5, ab v0.4.4).

        Auslöser ist twinraid-admin trust-host-key über das Signal SIGUSR2, nachdem der
        Verbindungstest mit dem Key „status“ bestanden ist. Eine laufende Sitzung bleibt
        unberührt; solange Groß offline ist, gilt weiter die TCP-Prüfung.
        """
        if self._process is not None:
            return
        self._plan.retry_now()
        self._log("vm-watch: waiting ended, connecting now")
        self._wakeup.set()

    async def run(self) -> None:
        while True:
            await self._wait_until_due()
            await self._session()

    def _files(self) -> FileSignature:
        return (_signature(self._target.key), _signature(self._target.known_hosts))

    def _set(self, view: WatchView) -> None:
        if view != self._view:
            self._view = view
            self.changed.set()

    def _log(self, message: str, *args: object) -> None:
        """Feste Codes, gleiche Meldungen höchstens einmal pro Minute."""
        key = message % args
        now = self._monotonic()
        if now - self._logged.get(key, -60.0) >= 60.0:
            self._logged[key] = now
            logger.info(message, *args)

    def _kill(self) -> None:
        if self._process is not None:
            _terminate(self._process)

    async def _wait_until_due(self) -> None:
        while True:
            self._wakeup.clear()
            now = self._monotonic()
            if self._online and self._plan.may_connect(now, self._files()):
                return
            delay = self._plan.wait_seconds(now) if self._online else None
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await self._wakeup.wait()

    async def _session(self) -> None:
        generation = self._generation
        missing = self._target.missing()
        if missing is not None:
            self._failed(_reason_of(missing.outcome), 0.0, booting=False)
            return
        self._set(WatchView(None, StatusReason.WATCH_CONNECTING))
        started = self._monotonic()
        arguments = ssh_arguments(self._target, [WATCH_REQUEST], self._binary)
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=SSH_ENVIRONMENT,
                limit=MAX_LINE_BYTES,
            )
        except OSError:
            self._failed(StatusReason.SSH_FAILED, 0.0, booting=False)
            return
        self._process = process
        stderr_task = asyncio.create_task(_read_limited(process.stderr))
        problem, received = None, False
        try:
            problem, received = await self._read_lines(process, generation)
        finally:
            self._process = None
            _terminate(process)
            await process.wait()
            stderr = await stderr_task
        if generation != self._generation:
            return  # Groß ist inzwischen offline; der Zustand ist schon zurückgesetzt.
        if problem is None:
            # Die Verbindung endete von selbst; der Exit-Code von ssh sagt, warum.
            error = classify(process.returncode or 0, stderr)
            problem = StatusReason.WATCH_INTERRUPTED if error is None else _reason_of(error.outcome)
        booting = problem in _BOOTING and not received
        self._failed(problem, self._monotonic() - started, booting)

    async def _read_lines(
        self, process: asyncio.subprocess.Process, generation: int
    ) -> tuple[StatusReason | None, bool]:
        """Liest Zeilen bis zum Ende; gibt einen erkannten Fehler zurück und ob Zeilen kamen."""
        received = False
        if process.stdout is None:  # pragma: no cover - mit PIPE gestartet
            return StatusReason.SSH_FAILED, received
        while True:
            try:
                async with asyncio.timeout(self._timings.stall_seconds):
                    raw = await process.stdout.readline()
            except TimeoutError, ValueError:  # hängend oder Zeile länger als MAX_LINE_BYTES
                return StatusReason.WATCH_INTERRUPTED, received
            if not raw:
                return None, received
            try:
                report = parse_line(raw)
            except WatchLineError as exc:
                outdated = StatusReason.GUARD_OUTDATED
                return (outdated if exc.outdated else StatusReason.WATCH_INTERRUPTED), received
            if generation != self._generation:
                return None, received
            if not received:
                self._log("vm-watch: session started")
                received = True
            self._set(WatchView(report))

    def _failed(self, reason: StatusReason, session_seconds: float, booting: bool) -> None:
        now = self._monotonic()
        if reason in PERMANENT_REASONS:
            self._plan.permanent(now, self._files())
        else:
            self._plan.transient(now, session_seconds, booting)
        shown, retry = reason, None
        if booting and self._plan.in_boot_window(now):
            # Groß startet gerade: Das ist kein Fehler, sshd kommt gleich.
            shown = StatusReason.WATCH_CONNECTING
        else:
            at = self._clock() + timedelta(seconds=self._plan.remaining(now))
            retry = WatchRetry(at=_rounded_up(at), paused=self._plan.paused)
        self._set(WatchView(None, shown, retry))
        self._log("vm-watch: session ended (%s)", reason.value)
