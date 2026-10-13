"""Live-Datenstrom (SSE) und Sitzungsregeln (Konzept 4.1 und 5.3)."""

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.web.helpers import (
    TEST_EVENT_TIMINGS,
    FakeClock,
    FakeWorker,
    login,
    make_app,
    make_client,
    unknown_status,
)
from twinraid.web import events
from twinraid.web.events import EventTimings, SessionCheck, StatusHub, event_stream
from twinraid.web.sessions import Lookup, Session

MINUTE = 60.0
# Genug Zeit für lange simulierte Abläufe (siehe SessionTimeline).
SLOW = EventTimings(
    poll_seconds=0.02, session_check_seconds=1.0, keepalive_seconds=1.0, max_stream_seconds=20.0
)


def _events(body: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def _names(body: str) -> list[str]:
    return [name for name, _ in _events(body)]


def _audit(tmp_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        return [(str(a), str(r)) for a, r in db.execute("SELECT action, result FROM audit_log")]


def _last_seen(tmp_path: Path) -> float | None:
    with sqlite3.connect(tmp_path / "web.sqlite") as db:
        row = db.execute("SELECT last_seen FROM sessions").fetchone()
    return float(row[0]) if row else None


def _setup(
    tmp_path: Path, timings: EventTimings = TEST_EVENT_TIMINGS
) -> tuple[FakeWorker, FakeClock, TestClient]:
    worker, clock = FakeWorker(), FakeClock()
    worker.stamp_checks = True
    client = make_client(make_app(tmp_path, worker, clock=clock, event_timings=timings))
    login(client)
    return worker, clock, client


def test_events_require_a_session(tmp_path: Path) -> None:
    assert make_client(make_app(tmp_path)).get("/api/events").status_code == 401


def test_stream_delivers_status_and_headers(tmp_path: Path) -> None:
    _worker, _clock, client = _setup(tmp_path)
    response = client.get("/api/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.text.startswith("retry: 5000\n\n")
    name, data = _events(response.text)[0]
    assert name == "status"
    assert isinstance(data, dict)
    assert {k: v for k, v in data.items() if k != "checked_at"} == {
        k: v for k, v in unknown_status().model_dump(mode="json").items() if k != "checked_at"
    }


def test_stream_reports_unavailable_worker(tmp_path: Path) -> None:
    worker, _clock, client = _setup(tmp_path)
    worker.available = False
    assert _names(client.get("/api/events").text)[0] == "worker_unavailable"


def test_opening_the_stream_does_not_extend_the_session(tmp_path: Path) -> None:
    """Auch Aufbau und Neuverbinden zählen nicht als Aktivität."""
    _worker, clock, client = _setup(tmp_path)
    logged_in = _last_seen(tmp_path)
    clock.advance(9 * MINUTE)
    client.get("/api/events")
    assert _last_seen(tmp_path) == logged_in


class SessionTimeline:
    """Simulierte Zeit, die bei jeder Sitzungsprüfung des Datenstroms vorrückt.

    Die Zeit läuft hier nicht bei den Statusabfragen weiter: Ist der Datenstrom
    langsamer als die Abfragen, bekommt er nur den neuesten Stand. Zwei Abfragen
    lägen dann ohne Prüfung dazwischen, und die simulierte Zeit spränge doppelt.
    In echter Zeit gibt es diesen Sprung nicht. So bleibt der Test unabhängig davon,
    wie schnell der Rechner ist.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
        self.clock = clock
        self.started = clock.now
        self.step = 0.0
        # (Zeitpunkt, touch, force, Session noch gültig) je Prüfung.
        self.checks: list[tuple[float, bool, bool, bool]] = []
        original = events._still_valid

        async def advancing(
            check: SessionCheck, touch: bool, force: bool, on_expired: Callable[[], Awaitable[None]]
        ) -> bool:
            clock.advance(self.step)
            valid = await original(check, touch, force, on_expired)
            self.checks.append((clock.now - self.started, touch, force, valid))
            return valid

        monkeypatch.setattr(events, "_still_valid", advancing)

    def elapsed(self) -> float:
        return self.clock.now - self.started

    def touches(self) -> list[float]:
        return [at for at, touch, _force, valid in self.checks if touch and valid]

    def forced(self) -> list[float]:
        return [at for at, _touch, force, valid in self.checks if force and valid]


def test_stream_ends_when_the_session_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _worker, clock, client = _setup(tmp_path, SLOW)
    timeline = SessionTimeline(monkeypatch, clock)
    timeline.step = 4 * MINUTE
    names = _names(client.get("/api/events").text)
    assert names[-1] == "session_ended"
    # Ohne Job keine Verlängerung: Ablauf bei der ersten Prüfung nach 10 Minuten.
    assert timeline.touches() == []
    assert timeline.elapsed() == 12 * MINUTE
    assert _last_seen(tmp_path) is None
    assert client.get("/api/status").status_code == 401
    assert ("session", "expired") in _audit(tmp_path)


def test_running_job_keeps_an_open_dashboard_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, clock, client = _setup(tmp_path, SLOW)
    timeline = SessionTimeline(monkeypatch, clock)
    timeline.step = 4 * MINUTE

    def job(_call: int) -> None:
        # Der Kopierjob läuft in den ersten 32 Minuten, also weit über 10 Minuten.
        worker.job_running = timeline.elapsed() < 32 * MINUTE

    worker.on_status = job
    events_seen = _events(client.get("/api/events").text)
    assert events_seen[-1][0] == "session_ended"
    assert any(isinstance(d, dict) and d["job_running"] for n, d in events_seen if n == "status")
    # Am Ende des Jobs wird die Aktivität einmal sofort gesetzt ...
    assert len(timeline.forced()) == 1
    job_end = timeline.forced()[0]
    assert job_end >= 32 * MINUTE
    # ... vorher hielt das offene Dashboard die Session, danach zählen die 10 Minuten neu.
    assert timeline.touches()[0] == 4 * MINUTE
    assert 10 * MINUTE < timeline.elapsed() - job_end <= 14 * MINUTE


def test_job_end_restarts_the_idle_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    short = EventTimings(
        poll_seconds=0.02, session_check_seconds=1.0, keepalive_seconds=1.0, max_stream_seconds=0.5
    )
    worker, clock, client = _setup(tmp_path, short)
    timeline = SessionTimeline(monkeypatch, clock)
    timeline.step = 3 * MINUTE

    def job(_call: int) -> None:
        # Job läuft 6 Minuten; nach seinem Ende steht die Zeit still.
        worker.job_running = timeline.elapsed() < 6 * MINUTE
        if timeline.forced():
            timeline.step = 0.0

    worker.on_status = job
    client.get("/api/events")
    # Die letzte Aktivität steht genau auf dem Ende des Jobs, die Session gilt weiter.
    assert len(timeline.forced()) == 1
    assert _last_seen(tmp_path) == timeline.started + timeline.forced()[0]
    assert client.get("/api/status").status_code == 200


def test_job_without_open_dashboard_does_not_keep_the_session(tmp_path: Path) -> None:
    worker, clock, client = _setup(tmp_path)
    worker.job_running = True
    clock.advance(10 * MINUTE + 1)
    assert client.get("/api/status").status_code == 401


def test_maximum_lifetime_applies_during_a_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, clock, client = _setup(tmp_path, SLOW)
    timeline = SessionTimeline(monkeypatch, clock)
    timeline.step = 9 * MINUTE
    worker.job_running = True
    names = _names(client.get("/api/events").text)
    assert names[-1] == "session_ended"
    # Bis zur Höchstdauer verlängert der Job die Session bei jeder Prüfung ...
    assert len(timeline.touches()) == 12 * 60 // 9 - 1
    # ... danach endet sie trotz laufendem Job.
    assert 12 * 60 * MINUTE <= timeline.elapsed() < 12 * 60 * MINUTE + 9 * MINUTE


def test_ending_a_session_sends_no_command_to_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logout oder Ablauf beenden nie den Kopierjob: Der Worker bekommt nur Abfragen."""
    worker, clock, client = _setup(tmp_path, SLOW)
    timeline = SessionTimeline(monkeypatch, clock)
    timeline.step = 20 * MINUTE
    worker.job_running = True
    assert _names(client.get("/api/events").text)[-1] == "session_ended"
    csrf = login(client)
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert set(worker.calls) == {"get_status", "verify_password"}


def test_logout_in_another_tab_ends_the_stream(tmp_path: Path) -> None:
    worker, _clock, client = _setup(tmp_path, SLOW)

    def logout_elsewhere(call: int) -> None:
        if call == 2:
            with sqlite3.connect(tmp_path / "web.sqlite") as db:
                db.execute("DELETE FROM sessions")
        worker.job_running = call % 2 == 0

    worker.on_status = logout_elsewhere
    assert _names(client.get("/api/events").text)[-1] == "session_ended"


@pytest.mark.anyio
async def test_hub_shares_one_poller_and_stops_without_streams() -> None:
    worker = FakeWorker()
    hub = StatusHub(worker, poll_seconds=0.01)
    first, second = hub.subscribe(), hub.subscribe()
    assert hub.stream_count == 2
    assert await first.get() == await second.get()
    await asyncio.sleep(0.05)
    hub.unsubscribe(first)
    hub.unsubscribe(second)
    calls = worker.status_calls
    await asyncio.sleep(0.05)
    assert worker.status_calls == calls


@pytest.mark.anyio
async def test_stream_touches_only_while_a_job_runs() -> None:
    worker = FakeWorker()
    worker.stamp_checks = True
    hub = StatusHub(worker, poll_seconds=0.01)
    checks: list[tuple[bool, bool]] = []
    session = Session("hash", "csrf", 0.0, 0.0)

    def toggle(call: int) -> None:
        worker.job_running = call in (2, 3)

    worker.on_status = toggle

    async def check(touch: bool, force: bool) -> Lookup:
        checks.append((touch, force))
        return Lookup(session)

    async def expired() -> None:
        return None

    timings = EventTimings(
        poll_seconds=0.02, session_check_seconds=5, keepalive_seconds=5, max_stream_seconds=0.3
    )
    async for _chunk in event_stream(hub, check, timings, expired):
        pass
    # Ohne Job nur prüfen, mit Job verlängern, am Ende des Jobs sofort verlängern,
    # danach wieder nur prüfen.
    end = checks.index((True, True))
    assert checks[0] == (False, False)
    assert (True, False) in checks[:end]
    assert set(checks[end + 1 :]) <= {(False, False)}
    assert checks.count((True, True)) == 1


def test_number_of_streams_is_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("twinraid.web.app.MAX_STREAMS", 0)
    _worker, _clock, client = _setup(tmp_path)
    response = client.get("/api/events")
    assert response.status_code == 503
    assert response.json() == {"detail": "too_many_streams"}
