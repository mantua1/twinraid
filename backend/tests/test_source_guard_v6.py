"""host-scripts/source-guard.sh Version 6 (Protokoll 5, Konzept 3.6, ab v0.6.1).

Benachrichtigungen über Unraids notify (Auftrag notify, eigene Meldungen des Wächters),
Lesezeichen aufräumen, Zeiten aus einem festen Abstand zwischen Uhr und Laufzeit und der
Neuversuch beim Verwerfen im Takt des ZFS-Teils. notify, zfs und ssh kommen aus
tests/support/fake_source_host.py; die Lesezeichen mit echtem ZFS prüft
docker/source-guard-sync-test.sh in der CI.
"""

import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import test_source_guard_jobs as jobs
from tests.support.source_guard_host import DEFAULT_CONF, SCRIPT, Host, make_host, order_line
from tests.test_source_guard_jobs import (
    BASE,
    BASE_GUID,
    CONF,
    DS,
    check_answer,
    prepared,
    sent,
    started,
)
from twinraid.shared.actions import ActionBlocker, ActionOutcome

# Fixture aus den Tests von Version 2: Host mit eigenen festen Werten, ohne Start.
make = jobs.make
NOTIFY_CONF = CONF + "notify on\n"
Status = dict[str, Any]


def _script_list(name: str) -> list[str]:
    match = re.search(rf"^readonly {name}=\((.*?)\)$", SCRIPT.read_text(), re.M | re.S)
    assert match is not None, name
    return match[1].split()


def notify_calls(host: Host) -> list[list[str]]:
    return host.calls("notify")


def notifying(make: Callable[..., Host], conf: str = NOTIFY_CONF, **constants: str) -> Host:
    host = make(**constants)
    host.configure(conf)
    return started(host)


# --- Listen und Konfiguration -----------------------------------------------------------------


def test_reasons_are_those_of_twinraid() -> None:
    """Die Gründe sind die Ergebnisse und Sperrgründe von TwinRaid ohne success (Konzept 3.6)."""
    expected = [o.value for o in ActionOutcome if o is not ActionOutcome.SUCCESS]
    expected += [b.value for b in ActionBlocker if b.value not in expected]
    assert _script_list("NOTIFY_REASONS") == expected


def test_events_are_fixed() -> None:
    assert _script_list("NOTIFY_EVENTS") == [
        "schedule_ok",
        "schedule_failed",
        "schedule_skipped",
        "schedule_missed",
        "auto_shutdown_failed",
    ]


def test_notify_is_off_without_the_line(make: Callable[..., Host]) -> None:
    host = started(make())
    assert host.status()["notify"] == "off"
    last = host.send("notify schedule_failed send_failed")
    assert (last["order"], last["container"], last["result"]) == ("notify", None, "notify_off")
    assert notify_calls(host) == []


@pytest.mark.parametrize(
    ("line", "detail"),
    [("notify yes\n", "bad_value"), ("notify on\nnotify off\n", "duplicate")],
)
def test_notify_line_is_checked(make: Callable[..., Host], line: str, detail: str) -> None:
    host = make()
    host.configure(CONF + line)
    host.start_run()
    status = host.wait_status(lambda s: s["error"] == "config_invalid")
    assert status["error_detail"] == detail
    assert status["notify"] == "off"


# --- Auftrag notify ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("order", "level", "subject", "text"),
    [
        (
            "schedule_ok -",
            "normal",
            "TwinRaid: Geplanter Sync erfolgreich",
            "TwinRaid meldet: Der geplante Sync ist erfolgreich durchgelaufen.",
        ),
        (
            "schedule_failed send_failed",
            "alert",
            "TwinRaid: Geplanter Sync gescheitert",
            "TwinRaid meldet: Der geplante Sync ist gescheitert (send_failed). "
            "Details in der Kachel Sync.",
        ),
        (
            "schedule_skipped source_guard_too_soon",
            "warning",
            "TwinRaid: Geplanter Sync ausgelassen",
            "TwinRaid meldet: Der geplante Sync wurde ausgelassen (source_guard_too_soon).",
        ),
        (
            "schedule_missed -",
            "warning",
            "TwinRaid: Geplanter Sync verpasst",
            "TwinRaid meldet: Ein geplanter Sync wurde verpasst, weil TwinRaid oder Klein zu der "
            "Zeit aus war.",
        ),
        (
            "auto_shutdown_failed other_vms_running",
            "warning",
            "TwinRaid: Groß nicht heruntergefahren",
            "TwinRaid meldet: Das automatische Herunterfahren von Groß ist gescheitert "
            "(other_vms_running); Groß bleibt an.",
        ),
    ],
)
def test_each_event_calls_notify_with_the_fixed_text(
    make: Callable[..., Host], order: str, level: str, subject: str, text: str
) -> None:
    host = notifying(make)
    assert host.status()["notify"] == "on"
    last = host.send(f"notify {order}")
    assert (last["order"], last["result"]) == ("notify", "notified")
    assert notify_calls(host) == [["-e", "TwinRaid", "-s", subject, "-d", text, "-i", level]]
    event, reason = order.split()
    assert f"order {last['seq']}: notify {event} {reason}: notified" in host.syslog()


@pytest.mark.parametrize(
    "order",
    [
        "notify schedule_done -",  # unbekanntes Ereignis
        "notify schedule_failed no_such_code",  # Grund nicht in der Liste
        "notify schedule_failed -",  # Grund fehlt
        "notify schedule_ok send_failed",  # Grund nicht erlaubt
        "notify schedule_missed busy",
        "notify deadman deadman_max",  # eigene Meldungen nur vom Wächter selbst
        "notify container_not_restarted gone",
    ],
)
def test_invalid_event_or_reason(make: Callable[..., Host], order: str) -> None:
    host = notifying(make)
    assert host.send(order)["result"] == "notify_invalid"
    assert notify_calls(host) == []


@pytest.mark.parametrize(
    "text",
    [
        "notify Schedule_ok -",
        "notify schedule_ok $(id)",
        "notify schedule_ok ;rm",
        "notify " + "a" * 33 + " -",
        "notify schedule_failed " + "a" * 41,
        "notify schedule_ok",
        "notify schedule_ok - extra",
    ],
)
def test_bad_lines_are_rejected_by_the_expression(make: Callable[..., Host], text: str) -> None:
    """Der eine reguläre Ausdruck lässt nur [a-z_] und - zu (Konzept 3.6, „Aufträge“)."""
    host = notifying(make)
    before = host.status()["last_seq"]
    host.write_order(order_line(before + 1, text))
    status = host.wait_status(lambda s: s["rejected"]["count"] >= 1)
    assert status["last_seq"] == before
    assert notify_calls(host) == []


def test_one_message_per_minute(make: Callable[..., Host]) -> None:
    host = notifying(make)
    assert host.send("notify schedule_missed -")["result"] == "notified"
    assert host.send("notify schedule_missed -")["result"] == "notify_limited"
    assert len(notify_calls(host)) == 1


def test_at_most_twenty_a_day(make: Callable[..., Host]) -> None:
    host = notifying(make, NOTIFY_GAP="0", NOTIFY_LIMIT="3")
    results = [host.send("notify schedule_missed -")["result"] for _ in range(4)]
    assert results == ["notified", "notified", "notified", "notify_limited"]
    assert len(notify_calls(host)) == 3
    # Die Grenze übersteht einen Neustart des Wächters (private/ bleibt).
    host.stop_run(host.processes[-1])
    host.start_run()
    assert host.send("notify schedule_missed -")["result"] == "notify_limited"


def test_failed_notify(make: Callable[..., Host]) -> None:
    host = notifying(make)
    host.flag("notify-fail")
    assert host.send("notify schedule_failed job_timeout")["result"] == "notify_failed"
    assert "notify schedule_failed: the notify script failed" in host.syslog()


def test_missing_notify_script(make: Callable[..., Host]) -> None:
    host = make()
    (host.tmp / "bin" / "notify").unlink()
    host.configure(NOTIFY_CONF)
    started(host)
    assert host.send("notify schedule_missed -")["result"] == "notify_failed"


def test_notify_does_not_touch_run_or_job(make: Callable[..., Host]) -> None:
    host = notifying(make)
    host.send("begin")
    assert host.send("notify schedule_missed -")["result"] == "notified"
    status = host.status()
    assert status["run"] is not None
    assert status["job"] is None
    assert host.send("finish")["result"] == "finished"


# --- Eigene Meldungen des Wächters -----------------------------------------------------------


def _deadman(host: Host) -> None:
    """Lauf mit angehaltenem Container; die Herzschlag-Frist liegt nach dem Neustart zurück."""
    host.send("begin")
    host.send("stop twinraid-testcontainer")
    host.stop_run(host.processes[-1])
    run_file = host.private / "run"
    uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
    lines = run_file.read_text().splitlines()
    run_file.write_text(
        "\n".join(
            f"heartbeat {uptime - 1}" if line.startswith("heartbeat ") else line for line in lines
        )
        + "\n"
    )
    host.start_run()
    host.wait_status(lambda s: s["last_run"] is not None)


def test_own_message_after_the_deadman(make: Callable[..., Host]) -> None:
    host = notifying(make)
    _deadman(host)
    calls = notify_calls(host)
    assert len(calls) == 1
    assert calls[0][:4] == ["-e", "TwinRaid", "-s", "TwinRaid-Wächter: Totmannschalter"]
    assert "(deadman_heartbeat)" in calls[0][5]
    assert calls[0][7] == "alert"
    assert "own notification deadman deadman_heartbeat: notified" in host.syslog()


def test_no_own_message_without_the_switch(make: Callable[..., Host]) -> None:
    host = started(make())
    _deadman(host)
    assert notify_calls(host) == []


def test_own_message_when_a_container_does_not_start(make: Callable[..., Host]) -> None:
    host = notifying(make)
    host.send("begin")
    host.send("stop twinraid-testcontainer")
    host.change_container("twinraid-testcontainer", start_fails=True)
    assert host.send("finish")["result"] == "finished"
    calls = notify_calls(host)
    assert [call[3] for call in calls] == ["TwinRaid-Wächter: Container läuft nicht wieder"]
    assert "(start_failed)" in calls[0][5]


def test_own_messages_have_their_own_counter(make: Callable[..., Host]) -> None:
    """Ein übernommener TwinRaid kann die eigenen Meldungen nicht aufbrauchen (Konzept 3.6)."""
    host = notifying(make, NOTIFY_LIMIT="1")
    assert host.send("notify schedule_missed -")["result"] == "notified"
    assert host.send("notify schedule_missed -")["result"] == "notify_limited"
    _deadman(host)
    assert [call[3] for call in notify_calls(host)] == [
        "TwinRaid: Geplanter Sync verpasst",
        "TwinRaid-Wächter: Totmannschalter",
    ]


# --- Lesezeichen aufräumen --------------------------------------------------------------------


def _bookmark(guid: str, txg: int, created: int | None) -> dict[str, Any]:
    values: dict[str, Any] = {"guid": guid, "createtxg": txg}
    if created is not None:
        values["creation"] = created
    return values


def _with_bookmarks(host: Host, oldest: str, newest: str = BASE_GUID) -> None:
    """Basis BASE gehalten (txg 900, Lesezeichen #BASE); dazu ältere Lesezeichen, eins mit
    einem anderen Namen und eins, das neuer ist als die Basis."""

    def change(zfs: dict[str, Any]) -> None:
        zfs["bookmarks"] = {
            f"{DS}#twinraid-20260901T000000Z": _bookmark("201", 100, 1_700_000_100),
            f"{DS}#twinraid-20260902T000000Z": _bookmark("202", 200, 1_700_000_200),
            # Dieselbe GUID wie der neueste Snapshot auf Groß (newest_guid): bleibt.
            f"{DS}#twinraid-20260903T000000Z": _bookmark(newest, 300, 1_700_000_150),
            # creation nicht lesbar: bleibt.
            f"{DS}#twinraid-20260904T000000Z": _bookmark("204", 400, None),
            # nicht älter als der älteste Snapshot auf Groß: bleibt (bei oldest 1700000250).
            f"{DS}#twinraid-20260905T000000Z": _bookmark("205", 500, 1_800_000_000),
            # fremder Name: bleibt immer.
            f"{DS}#handmade": _bookmark("206", 600, 1_700_000_000),
            f"{DS}#{BASE}": _bookmark(BASE_GUID, 900, 1_700_000_900),
        }

    host.update_zfs(change)
    mirror = (
        f"state=mirror props=ok count=3 newest_guid={newest} newest_created=1760000000 "
        f"newest_received=1760000100 oldest_created={oldest}"
    )
    host.answer_ssh(check=check_answer({"twinraidtest": mirror}), receive={"default": {"rc": 0}})


def _bookmark_destroys(host: Host) -> list[list[str]]:
    return [call for call in host.calls("zfs") if call[0] == "destroy" and "#" in call[-1]]


def _restart(host: Host) -> None:
    host.stop_run(host.processes[-1])
    host.start_run()
    host.wait_status(lambda s: s["error"] is None and s["zfs"]["state"] == "ok")


def test_bookmarks_older_than_the_mirror_are_destroyed_after_sending(
    make: Callable[..., Host],
) -> None:
    host = started(make())
    _with_bookmarks(host, oldest="1700000250")
    job = prepared(host)
    assert job["state"] == "prepared"
    assert sent(host)["result"] == "ok"
    remaining = set(host.zfs()["bookmarks"])
    assert f"{DS}#twinraid-20260901T000000Z" not in remaining
    assert f"{DS}#twinraid-20260902T000000Z" not in remaining
    for kept in ("20260903T000000Z", "20260904T000000Z", "20260905T000000Z"):
        assert f"{DS}#twinraid-{kept}" in remaining
    assert f"{DS}#handmade" in remaining
    assert f"{DS}#{BASE}" in remaining
    # Das Lesezeichen des gesendeten Snapshots ist das neueste und bleibt.
    assert f"{DS}#{job['snapshot']}" in remaining
    assert _bookmark_destroys(host) == [
        ["destroy", "--", f"{DS}#twinraid-20260901T000000Z"],
        ["destroy", "--", f"{DS}#twinraid-20260902T000000Z"],
    ]
    assert "bookmarks: 2 own bookmarks destroyed, 0 failed" in host.syslog()


def test_bookmark_of_the_held_base_and_the_newest_stay(make: Callable[..., Host]) -> None:
    """Gehaltene Basis und neuestes Lesezeichen bleiben, auch wenn sie älter sind als der
    älteste Snapshot auf Groß. Hier über den täglichen Durchgang nach einem Neustart."""
    host = started(make())
    _with_bookmarks(host, oldest="1900000000", newest="999")
    host.update_zfs(
        lambda zfs: zfs["bookmarks"].update(
            {f"{DS}#twinraid-20261002T000000Z": _bookmark("207", 950, 1_700_001_000)}
        )
    )
    prepared(host)
    host.send("close")
    _restart(host)
    deadline = time.monotonic() + 8
    while len(_bookmark_destroys(host)) < 3 and time.monotonic() < deadline:
        time.sleep(0.1)
    remaining = set(host.zfs()["bookmarks"])
    assert f"{DS}#{BASE}" in remaining  # GUID der gehaltenen Basis
    assert f"{DS}#twinraid-20261002T000000Z" in remaining  # das neueste
    assert f"{DS}#twinraid-20260903T000000Z" in remaining  # GUID von newest_guid
    assert f"{DS}#twinraid-20260904T000000Z" in remaining  # creation nicht lesbar
    assert f"{DS}#handmade" in remaining
    assert [call[-1] for call in _bookmark_destroys(host)] == [
        f"{DS}#twinraid-20260901T000000Z",
        f"{DS}#twinraid-20260902T000000Z",
        f"{DS}#twinraid-20260905T000000Z",
    ]


@pytest.mark.parametrize(
    "mirror",
    [
        "state=mirror props=ok count=3 newest_guid=999 newest_created=1 newest_received=1 "
        "oldest_created=-",
        "state=absent props=- count=0 newest_guid=- newest_created=- newest_received=- "
        "oldest_created=-",
        "state=mirror props=ok count=3 newest_guid=- newest_created=1 newest_received=1 "
        "oldest_created=1900000000",
    ],
)
def test_nothing_is_destroyed_without_the_needed_values(
    make: Callable[..., Host], mirror: str
) -> None:
    host = started(make())
    _with_bookmarks(host, oldest="1900000000")
    host.answer_ssh(check=check_answer({"twinraidtest": mirror}), receive={"default": {"rc": 0}})
    prepared(host)
    host.send("close")
    _restart(host)
    time.sleep(1)
    assert _bookmark_destroys(host) == []


def test_nothing_is_destroyed_without_a_successful_check(make: Callable[..., Host]) -> None:
    host = started(make())
    _with_bookmarks(host, oldest="1900000000", newest="999")
    host.answer_ssh(check={"rc": 255, "stderr": "ssh: connect to host: Connection refused"})
    prepared(host)
    host.send("close")
    _restart(host)
    time.sleep(1)
    assert _bookmark_destroys(host) == []


def test_daily_cleanup_uses_the_saved_check(make: Callable[..., Host]) -> None:
    """Nach einem Neustart läuft die Aufbewahrung sofort, mit dem gespeicherten check."""
    host = started(make())
    _with_bookmarks(host, oldest="1700000250")
    prepared(host)
    host.send("close")
    assert _bookmark_destroys(host) == []
    _restart(host)
    deadline = time.monotonic() + 8
    while len(_bookmark_destroys(host)) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert [call[-1] for call in _bookmark_destroys(host)] == [
        f"{DS}#twinraid-20260901T000000Z",
        f"{DS}#twinraid-20260902T000000Z",
    ]


def test_failed_bookmark_destroy_is_logged(make: Callable[..., Host]) -> None:
    host = started(make())
    _with_bookmarks(host, oldest="1700000250")
    host.update_zfs(lambda zfs: zfs.setdefault("fail", []).append("destroy_bookmark"))
    prepared(host)
    sent(host)
    assert "bookmarks: 0 own bookmarks destroyed, 2 failed" in host.syslog()
    assert len(host.zfs()["bookmarks"]) == 8


# --- Verwerfen im Takt des ZFS-Teils ---------------------------------------------------------


def test_discard_retries_at_the_pace_of_the_zfs_part(make: Callable[..., Host]) -> None:
    host = started(make(ZFS_INTERVAL="3"))
    prepared(host)
    host.update_zfs(lambda zfs: zfs.setdefault("fail", []).append("get_snapshots"))
    before = len(host.calls("zfs"))
    host.send("close")
    time.sleep(2)
    lists = [
        call
        for call in host.calls("zfs")[before:]
        if call[:2] == ["get", "-H"] and "snapshot" in call
    ]
    # Bis Version 5 kam ein Versuch in jeder Runde (hier alle 0,05 s).
    assert 1 <= len(lists) <= 3
    host.update_zfs(lambda zfs: zfs["fail"].remove("get_snapshots"))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not any(
        "provisional snapshot" in line and "discarded on 1 datasets" in line
        for line in host.syslog()
    ):
        time.sleep(0.1)
    assert any("discarded on 1 datasets" in line for line in host.syslog())


# --- Fester Abstand zwischen Uhr und Laufzeit ----------------------------------------------


class Uptime:
    """/proc/uptime zum Mitlesen: folgt der Uhr mit einem einstellbaren Versatz."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.shift = 0.0
        self._stop = threading.Event()
        self._write()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _write(self) -> None:
        value = time.time() - 1_700_000_000 + self.shift
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(f"{value:.2f} 0.00\n")
        tmp.replace(self.path)

    def _loop(self) -> None:
        while not self._stop.wait(0.01):
            self._write()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()


@pytest.fixture
def uptime(tmp_path: Path) -> Iterator[Uptime]:
    clock = Uptime(tmp_path / "uptime")
    yield clock
    clock.stop()


def test_times_do_not_jitter(make: Callable[..., Host], uptime: Uptime) -> None:
    host = started(make(UPTIME=str(uptime.path)))
    host.send("begin")
    host.send("finish")
    seen = set()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = host.status()
        seen.add(status["run_allowed_at"])
        time.sleep(0.05)
    assert len(seen) == 1, seen
    assert None not in seen


def test_a_changed_clock_moves_the_times(make: Callable[..., Host], uptime: Uptime) -> None:
    host = started(make(UPTIME=str(uptime.path)))
    host.send("begin")
    host.send("finish")
    first = host.status()["run_allowed_at"]
    assert first is not None
    # Die Laufzeit springt 5 s vor, als würde die Uhr um 5 s zurückgestellt: Die Frist in
    # Laufzeit bleibt, ihre Uhrzeit im Status kommt 5 s früher.
    uptime.shift = 5.0
    status = host.wait_status(lambda s: s["run_allowed_at"] != first, timeout=6)
    assert status["run_allowed_at"] is not None
    assert first - status["run_allowed_at"] in (4, 5, 6)
    assert any(line.startswith("clock changed by ") for line in host.syslog())


def test_one_second_of_jitter_does_not_count(make: Callable[..., Host], uptime: Uptime) -> None:
    host = started(make(UPTIME=str(uptime.path)))
    uptime.shift = 1.0
    time.sleep(2)
    assert not any(line.startswith("clock changed by ") for line in host.syslog())


def test_default_conf_example_has_the_notify_line() -> None:
    example = (SCRIPT.parent / "source-guard.conf.example").read_text()
    assert "\n# notify on\n" in example
    # Die Vorlage gilt ohne die Zeile: aus.
    assert "\nnotify " not in example


def test_make_host_knows_notify(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    assert (host.tmp / "bin" / "notify").exists()
    assert DEFAULT_CONF in host.conf.read_text()
