"""Termine des Zeitplans als reine Funktionen und der zuletzt behandelte Termin (Konzept 4.8).

Ein Termin ist ein Tag aus days mit der Uhrzeit time als Ortszeit in timezone. Der Worker
rechnet ihn in einen festen Zeitpunkt (UTC) um und behandelt jeden Termin höchstens einmal.

Zeitumstellung (Entscheidung E29):
- Gibt es die Uhrzeit an diesem Tag nicht (Umstellung auf Sommerzeit), gilt der Zeitpunkt
  des Sprungs, also der erste Zeitpunkt nach der Lücke, wie bei cron.
- Gibt es sie zweimal (Umstellung auf Winterzeit), gilt nur das erste Mal.
Beides gilt für jede Zeitzone, auch für eine mit 30 Minuten Umstellung.

Der zuletzt behandelte Termin steht in /data/worker/schedule.json (root, 0600, nur der Worker
schreibt). Nur Termine danach zählen; so überlebt die Regel einen Neustart und einen Sprung
der Uhr zurück.
"""

import contextlib
import itertools
import json
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from twinraid.config.models import ScheduleConfig

# Kürzel aus twinraid.yaml in der Reihenfolge von date.weekday() (Montag = 0).
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Ein verpasster Termin wird nur so lange nachgeholt (Konzept 4.8).
CATCH_UP = timedelta(hours=6)
# Für den kürzesten Abstand zwischen zwei Terminen: ein Jahr mit allen Zeitumstellungen.
_GAP_HORIZON = timedelta(days=371)
# schedule.json ist klein; größere Dateien sind sicher ein Fehler.
_MAX_STATE_BYTES = 4096


def _wall(day: date, time: str) -> datetime:
    hour, minute = (int(part) for part in time.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001 - Ortszeit


def _local(moment: datetime, zone: ZoneInfo) -> datetime:
    """Ortszeit als naive Uhrzeit (zum Vergleich mit der Uhrzeit des Plans)."""
    return moment.astimezone(zone).replace(tzinfo=None)


def term_on(day: date, time: str, zone: ZoneInfo) -> datetime:
    """Der Termin an diesem Tag in UTC, mit der Regel zur Zeitumstellung."""
    wall = _wall(day, time)
    # fold=0: bei einer doppelten Uhrzeit das erste Mal.
    first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if _local(first, zone) == wall:
        return first
    # Die Uhrzeit fällt in eine Lücke. fold=0 und fold=1 liegen auf beiden Seiten des
    # Sprungs; gesucht ist der erste Zeitpunkt, dessen Ortszeit die Uhrzeit erreicht hat.
    other = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    low, high = min(first, other), max(first, other)
    # Die Ortszeit steigt auch über einen Sprung nach vorn monoton: halbieren auf die Sekunde.
    low_s, high_s = int(low.timestamp()), int(high.timestamp())
    while low_s < high_s:
        middle = (low_s + high_s) // 2
        if _local(datetime.fromtimestamp(middle, UTC), zone) >= wall:
            high_s = middle
        else:
            low_s = middle + 1
    return datetime.fromtimestamp(low_s, UTC)


def _terms_from(schedule: ScheduleConfig, first_day: date) -> Iterator[datetime]:
    """Alle Termine ab first_day (Ortsdatum), aufsteigend, ohne Ende."""
    zone = ZoneInfo(schedule.timezone)
    days = {WEEKDAYS.index(day) for day in schedule.days}
    day = first_day
    while True:
        if day.weekday() in days:
            yield term_on(day, schedule.time, zone)
        day += timedelta(days=1)


def terms_between(schedule: ScheduleConfig, after: datetime, until: datetime) -> list[datetime]:
    """Termine t mit after < t <= until, aufsteigend."""
    zone = ZoneInfo(schedule.timezone)
    # Einen Tag früher beginnen: Ein Sprung kann einen Termin vor Mitternacht verschieben.
    first_day = after.astimezone(zone).date() - timedelta(days=1)
    found: list[datetime] = []
    for term in _terms_from(schedule, first_day):
        if term > until:
            break
        if term > after:
            found.append(term)
    return found


def next_terms(schedule: ScheduleConfig, after: datetime, count: int) -> list[datetime]:
    """Die nächsten count Termine nach after (Anzeige, Konzept 4.1 und 4.8)."""
    zone = ZoneInfo(schedule.timezone)
    first_day = after.astimezone(zone).date() - timedelta(days=1)
    found: list[datetime] = []
    for term in _terms_from(schedule, first_day):
        if term > after:
            found.append(term)
            if len(found) == count:
                break
    return found


@dataclass(frozen=True)
class Due:
    """Ein fälliger Termin; missed: er liegt mehr als 6 Stunden zurück."""

    term: datetime
    missed: bool


def due_term(schedule: ScheduleConfig, last_handled: datetime, now: datetime) -> Due | None:
    """Der jüngste Termin nach last_handled, der erreicht ist (Konzept 4.8).

    Mehrere verpasste Termine ergeben höchstens einen Lauf, mit dem jüngsten Termin. Er wird
    nachgeholt, wenn er höchstens 6 Stunden zurückliegt; der nächste Termin ist dann noch
    nicht erreicht, weil er der jüngste ist. Sonst gilt er als verpasst.
    """
    terms = terms_between(schedule, last_handled, now)
    if not terms:
        return None
    term = terms[-1]
    return Due(term=term, missed=now - term > CATCH_UP)


def shortest_gap(schedule: ScheduleConfig, after: datetime) -> timedelta:
    """Kürzester Abstand zwischen zwei Terminen im nächsten Jahr (Konzept 4.8, „Takt“).

    Ein Jahr deckt alle Zeitumstellungen ab; der Abstand kann deshalb kürzer als ein Tag sein.
    """
    terms = terms_between(schedule, after, after + _GAP_HORIZON)
    gaps = [later - earlier for earlier, later in itertools.pairwise(terms)]
    return min(gaps)


def format_term(term: datetime, timezone: str) -> str:
    """Für das Startlog: „Sun 2026-11-29 03:00 Europe/Berlin (02:00 UTC)“ (Konzept 4.8)."""
    local = term.astimezone(ZoneInfo(timezone))
    weekday = WEEKDAYS[local.weekday()].capitalize()
    utc = term.astimezone(UTC)
    return f"{weekday} {local:%Y-%m-%d %H:%M} {timezone} ({utc:%H:%M} UTC)"


class ScheduleStateError(Exception):
    """schedule.json ist vorhanden, aber nicht lesbar oder ungültig."""


class ScheduleState:
    """Der zuletzt behandelte Termin in /data/worker/schedule.json (Konzept 4.8 und 5.9).

    Ein Termin gilt als behandelt, sobald der Worker ihn angeht (Lauf, Auslassen oder
    verpasst); ein Neustart mitten im Lauf holt ihn so nicht noch einmal nach.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> datetime | None:
        """None: Die Datei fehlt. ScheduleStateError: vorhanden, aber nicht lesbar."""
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ScheduleStateError(type(exc).__name__) from None
        try:
            raw = os.read(fd, _MAX_STATE_BYTES + 1)
        except OSError as exc:
            raise ScheduleStateError(type(exc).__name__) from None
        finally:
            os.close(fd)
        if len(raw) > _MAX_STATE_BYTES:
            raise ScheduleStateError("too large")
        try:
            data = json.loads(raw)
            value = data["last_handled"]
            moment = datetime.fromisoformat(value)
        except ValueError, KeyError, TypeError:
            raise ScheduleStateError("invalid content") from None
        if not isinstance(value, str) or moment.tzinfo is None:
            raise ScheduleStateError("invalid content")
        return moment.astimezone(UTC)

    def save(self, term: datetime) -> None:
        """Atomar: temporäre Datei im selben Ordner, dann umbenennen. Wirft OSError."""
        value = term.astimezone(UTC).isoformat().replace("+00:00", "Z")
        data = json.dumps({"last_handled": value}, separators=(",", ":")) + "\n"
        temp = self._path.with_name(f".{self._path.name}.{secrets.token_hex(4)}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temp, flags, 0o600)
        try:
            try:
                os.write(fd, data.encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            temp.replace(self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise
