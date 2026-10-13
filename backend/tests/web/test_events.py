"""Live-Datenstrom (SSE) und Sitzungsregeln (Konzept 4.1 und 5.3)."""

import asyncio
import json
import sqlite3
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
from twinraid.web.events import EventTimings, StatusHub, event_stream
from twinraid.web.sessions import Lookup, Session

MINUTE = 60.0
# Etwas langsamere Abfragen, damit jeder Status einzeln ankommt, und genug Zeit für
# lange simulierte Abläufe. Die simulierte Zeit läuft nur bei Statusabfragen weiter.
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


def test_stream_ends_when_the_session_expires(tmp_path: Path) -> None:
    worker, clock, client = _setup(tmp_path, SLOW)
    # Jede Statusabfrage lässt 4 Minuten vergehen; ohne Job endet die Session nach 10.
    worker.on_status = lambda _call: clock.advance(4 * MINUTE)
    names = _names(client.get("/api/events").text)
    assert names[-1] == "session_ended"
    assert _last_seen(tmp_path) is None
    assert client.get("/api/status").status_code == 401


def test_running_job_keeps_an_open_dashboard_logged_in(tmp_path: Path) -> None:
    worker, clock, client = _setup(tmp_path, SLOW)
    started = clock.now
    job_end: list[float] = []

    def tick(call: int) -> None:
        clock.advance(4 * MINUTE)
        # Abfragen 1 bis 8: Kopierjob läuft (32 Minuten, also weit über 10 Minuten).
        worker.job_running = call <= 8
        if call == 9:
            job_end.append(clock.now)

    worker.on_status = tick
    events = _events(client.get("/api/events").text)
    assert events[-1][0] == "session_ended"
    statuses = [data for name, data in events if name == "status"]
    assert sum(1 for s in statuses if isinstance(s, dict) and s["job_running"]) == 8
    assert job_end == [started + 36 * MINUTE]
    # Nach dem Ende des Jobs beginnen die 10 Minuten neu: Ablauf erst danach.
    assert clock.now - job_end[0] >= 10 * MINUTE


def test_job_end_restarts_the_idle_time(tmp_path: Path) -> None:
    short = EventTimings(
        poll_seconds=0.02, session_check_seconds=1.0, keepalive_seconds=1.0, max_stream_seconds=0.5
    )
    worker, clock, client = _setup(tmp_path, short)
    logged_in = clock.now

    def tick(call: int) -> None:
        # Drei Abfragen à 3 Minuten, die ersten beiden mit laufendem Job; danach steht die Zeit.
        if call <= 3:
            clock.advance(3 * MINUTE)
        worker.job_running = call <= 2

    worker.on_status = tick
    client.get("/api/events")
    # Die letzte Aktivität steht auf dem Ende des Jobs (9 Minuten nach dem Login).
    assert _last_seen(tmp_path) == logged_in + 9 * MINUTE


def test_job_without_open_dashboard_does_not_keep_the_session(tmp_path: Path) -> None:
    worker, clock, client = _setup(tmp_path)
    worker.job_running = True
    clock.advance(10 * MINUTE + 1)
    assert client.get("/api/status").status_code == 401


def test_maximum_lifetime_applies_during_a_job(tmp_path: Path) -> None:
    worker, clock, client = _setup(tmp_path, SLOW)
    worker.job_running = True
    started = clock.now
    worker.on_status = lambda _call: clock.advance(9 * MINUTE)
    names = _names(client.get("/api/events").text)
    assert names[-1] == "session_ended"
    lifetime = clock.now - started
    assert 12 * 60 * MINUTE <= lifetime < 12 * 60 * MINUTE + 10 * MINUTE


def test_ending_a_session_sends_no_command_to_the_worker(tmp_path: Path) -> None:
    """Logout oder Ablauf beenden nie den Kopierjob: Der Worker bekommt nur Abfragen."""
    worker, clock, client = _setup(tmp_path, SLOW)
    worker.job_running = True
    worker.on_status = lambda _call: clock.advance(20 * MINUTE)
    assert _names(client.get("/api/events").text)[-1] == "session_ended"
    worker.on_status = None
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
