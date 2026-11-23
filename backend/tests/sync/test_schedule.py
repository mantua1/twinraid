"""Termine des Zeitplans als reine Funktionen (Konzept 4.8, Entscheidung E29)."""

import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import EXAMPLE_CONFIG
from twinraid.config import load_config
from twinraid.config.models import ScheduleConfig
from twinraid.sync.schedule import (
    CATCH_UP,
    ScheduleState,
    ScheduleStateError,
    due_term,
    format_term,
    next_terms,
    shortest_gap,
    term_on,
    terms_between,
)

BERLIN = ZoneInfo("Europe/Berlin")
# Lord Howe Island stellt die Uhr nur um 30 Minuten um (02:00 → 02:30 und zurück).
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")


def _schedule(
    days: list[str] | None = None, time: str = "03:00", timezone: str = "Europe/Berlin"
) -> ScheduleConfig:
    return ScheduleConfig(
        enabled=True,
        timezone=timezone,
        days=days or ["sun"],  # type: ignore[arg-type]
        time=time,
        wake_target=True,
        shutdown_after=True,
    )


def _utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- Termin an einem Tag ---


def test_normal_term_in_winter_and_summer() -> None:
    assert term_on(date(2026, 11, 29), "03:00", BERLIN) == _utc(2026, 11, 29, 2, 0)
    assert term_on(date(2027, 7, 4), "03:00", BERLIN) == _utc(2027, 7, 4, 1, 0)


@pytest.mark.parametrize("time", ["02:00", "02:01", "02:30", "02:59"])
def test_missing_time_in_march_runs_at_the_jump(time: str) -> None:
    """Umstellung auf Sommerzeit: 02:00-02:59 fehlt, der Lauf startet um 03:00 Sommerzeit."""
    term = term_on(date(2027, 3, 28), time, BERLIN)
    assert term == _utc(2027, 3, 28, 1, 0)
    assert term.astimezone(BERLIN).strftime("%H:%M %Z") == "03:00 CEST"


def test_times_around_the_gap_are_unchanged() -> None:
    assert term_on(date(2027, 3, 28), "01:59", BERLIN) == _utc(2027, 3, 28, 0, 59)
    assert term_on(date(2027, 3, 28), "03:00", BERLIN) == _utc(2027, 3, 28, 1, 0)
    assert term_on(date(2027, 3, 28), "03:01", BERLIN) == _utc(2027, 3, 28, 1, 1)


@pytest.mark.parametrize("time", ["02:00", "02:30", "02:59"])
def test_doubled_time_in_october_runs_the_first_time(time: str) -> None:
    """Umstellung auf Winterzeit: 02:00-02:59 gibt es zweimal, es gilt das erste Mal (CEST)."""
    term = term_on(date(2027, 10, 31), time, BERLIN)
    assert term.astimezone(BERLIN).strftime("%H:%M %Z") == f"{time} CEST"
    assert term == _utc(2027, 10, 31, 0, int(time[3:]))


def test_thirty_minute_shift_forward_and_back() -> None:
    # Vorwärts am 04.10.2026: 02:00 → 02:30; 02:10 gibt es nicht.
    gap = term_on(date(2026, 10, 4), "02:10", LORD_HOWE)
    assert gap.astimezone(LORD_HOWE).strftime("%H:%M") == "02:30"
    assert gap == term_on(date(2026, 10, 4), "02:29", LORD_HOWE)
    assert term_on(date(2026, 10, 4), "02:30", LORD_HOWE) == gap
    # Zurück am 04.04.2027: 01:30-01:59 gibt es zweimal, es gilt das erste Mal (+11).
    double = term_on(date(2027, 4, 4), "01:45", LORD_HOWE)
    assert double.astimezone(LORD_HOWE).utcoffset() == timedelta(hours=11)
    assert double == _utc(2027, 4, 3, 14, 45)


# --- Tage und Folgen von Terminen ---


def test_only_configured_days() -> None:
    schedule = _schedule(["mon", "thu"], "22:15")
    terms = terms_between(schedule, _utc(2026, 11, 23, 0, 0), _utc(2026, 12, 7, 0, 0))
    local = [term.astimezone(BERLIN).strftime("%a %d. %H:%M") for term in terms]
    assert local == ["Mon 23. 22:15", "Thu 26. 22:15", "Mon 30. 22:15", "Thu 03. 22:15"]


def test_terms_between_excludes_start_and_includes_end() -> None:
    schedule = _schedule()
    term = _utc(2026, 11, 29, 2, 0)
    assert terms_between(schedule, term, term + timedelta(days=1)) == []
    assert terms_between(schedule, term - timedelta(seconds=1), term) == [term]


def test_next_terms() -> None:
    schedule = _schedule(["sun"], "03:00")
    terms = next_terms(schedule, _utc(2027, 3, 20, 12, 0), 3)
    # Der zweite fällt auf die Zeitumstellung, 03:00 gibt es dann als Sommerzeit.
    assert terms == [_utc(2027, 3, 21, 2, 0), _utc(2027, 3, 28, 1, 0), _utc(2027, 4, 4, 1, 0)]


def test_next_terms_in_utc() -> None:
    schedule = _schedule(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "00:00", "UTC")
    assert next_terms(schedule, _utc(2026, 11, 23, 0, 0), 2) == [
        _utc(2026, 11, 24, 0, 0),
        _utc(2026, 11, 25, 0, 0),
    ]


# --- Fälliger Termin, nie zweimal, Nachholen ---


def test_due_term_at_its_time() -> None:
    schedule = _schedule()
    term = _utc(2026, 11, 29, 2, 0)
    last = _utc(2026, 11, 23, 12, 0)
    assert due_term(schedule, last, term - timedelta(seconds=1)) is None
    due = due_term(schedule, last, term + timedelta(seconds=10))
    assert due is not None
    assert (due.term, due.missed) == (term, False)


def test_never_twice() -> None:
    """Ist der Termin behandelt, gilt er nie wieder, auch nicht nach einem Sprung der Uhr."""
    schedule = _schedule()
    term = _utc(2026, 11, 29, 2, 0)
    assert due_term(schedule, term, term + timedelta(minutes=5)) is None
    # Die Uhr springt zurück vor den Termin und läuft wieder darüber hinweg.
    assert due_term(schedule, term, term - timedelta(hours=1)) is None
    assert due_term(schedule, term, term + timedelta(seconds=1)) is None


def test_doubled_hour_runs_only_once() -> None:
    """Nach dem ersten 02:30 kommt 02:30 Winterzeit; es ist kein neuer Termin."""
    schedule = _schedule(time="02:30")
    first = _utc(2027, 10, 31, 0, 30)
    second_wall = _utc(2027, 10, 31, 1, 30)
    assert second_wall.astimezone(BERLIN).strftime("%H:%M %Z") == "02:30 CET"
    assert due_term(schedule, first, second_wall + timedelta(minutes=1)) is None


def test_catch_up_within_six_hours() -> None:
    schedule = _schedule()
    term = _utc(2026, 11, 29, 2, 0)
    due = due_term(schedule, _utc(2026, 11, 22, 2, 0), term + CATCH_UP)
    assert due is not None
    assert (due.term, due.missed) == (term, False)


def test_missed_after_six_hours() -> None:
    schedule = _schedule()
    term = _utc(2026, 11, 29, 2, 0)
    due = due_term(schedule, _utc(2026, 11, 22, 2, 0), term + CATCH_UP + timedelta(seconds=1))
    assert due is not None
    assert (due.term, due.missed) == (term, True)


def test_several_missed_terms_give_one_with_the_latest() -> None:
    """TwinRaid war mehrere Tage aus: nur der jüngste Termin zählt."""
    schedule = _schedule(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    last = _utc(2026, 11, 20, 2, 0)
    due = due_term(schedule, last, _utc(2026, 11, 24, 4, 0))
    assert due is not None
    assert (due.term, due.missed) == (_utc(2026, 11, 24, 2, 0), False)
    due = due_term(schedule, last, _utc(2026, 11, 24, 12, 0))
    assert due is not None
    assert (due.term, due.missed) == (_utc(2026, 11, 24, 2, 0), True)


def test_clock_jumps_forward() -> None:
    """Die Uhr springt über einen Termin: innerhalb von 6 h nachholen, sonst verpasst."""
    schedule = _schedule()
    term = _utc(2026, 11, 29, 2, 0)
    last = term - timedelta(days=7)
    due = due_term(schedule, last, term + timedelta(hours=2))
    assert due is not None
    assert due.missed is False
    due = due_term(schedule, last, term + timedelta(hours=7))
    assert due is not None
    assert due.missed is True


# --- Kürzester Abstand und Anzeige ---


def test_shortest_gap_daily_includes_the_shift() -> None:
    """Täglich 03:00: um die Zeitumstellung liegen 23 Stunden zwischen zwei Terminen."""
    daily = _schedule(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    assert shortest_gap(daily, _utc(2026, 11, 23, 0, 0)) == timedelta(hours=23)
    utc = _schedule(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "03:00", "UTC")
    assert shortest_gap(utc, _utc(2026, 11, 23, 0, 0)) == timedelta(days=1)
    assert shortest_gap(_schedule(), _utc(2026, 11, 23, 0, 0)) >= timedelta(days=7) - timedelta(
        hours=1
    )


def test_format_term_for_the_start_log() -> None:
    term = _utc(2026, 11, 29, 2, 0)
    assert format_term(term, "Europe/Berlin") == "Sun 2026-11-29 03:00 Europe/Berlin (02:00 UTC)"


def test_example_schedule_is_sunday_three() -> None:
    schedule = load_config(EXAMPLE_CONFIG).sync.schedule
    assert next_terms(schedule, _utc(2026, 11, 23, 0, 0), 1) == [_utc(2026, 11, 29, 2, 0)]


# --- schedule.json ---


def test_state_missing_is_none(tmp_path: Path) -> None:
    assert ScheduleState(tmp_path / "schedule.json").load() is None


def test_state_round_trip_and_rights(tmp_path: Path) -> None:
    path = tmp_path / "schedule.json"
    state = ScheduleState(path)
    term = _utc(2026, 11, 29, 2, 0)
    state.save(term)
    assert state.load() == term
    assert path.read_text() == '{"last_handled":"2026-11-29T02:00:00Z"}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Keine temporären Dateien bleiben zurück.
    assert [entry.name for entry in tmp_path.iterdir()] == ["schedule.json"]
    state.save(term + timedelta(days=7))
    assert state.load() == term + timedelta(days=7)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{",
        "[]",
        '{"last": "2026-11-29T02:00:00Z"}',
        '{"last_handled": 5}',
        '{"last_handled": "gestern"}',
        '{"last_handled": "2026-11-29T02:00:00"}',
        "x" * 5000,
    ],
)
def test_state_invalid_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "schedule.json"
    path.write_text(content)
    with pytest.raises(ScheduleStateError):
        ScheduleState(path).load()


def test_state_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text('{"last_handled":"2026-11-29T02:00:00Z"}')
    path = tmp_path / "schedule.json"
    path.symlink_to(target)
    with pytest.raises(ScheduleStateError):
        ScheduleState(path).load()
    # Speichern ersetzt den Link selbst durch eine Datei, nie sein Ziel.
    ScheduleState(path).save(_utc(2026, 12, 6, 2, 0))
    assert not path.is_symlink()
    assert target.read_text() == '{"last_handled":"2026-11-29T02:00:00Z"}'
    assert ScheduleState(path).load() == _utc(2026, 12, 6, 2, 0)
