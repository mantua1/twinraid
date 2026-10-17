"""Sitzung vm-watch: Zeilenformat, Neuversuche und Ablauf (Konzept 3.5, ab v0.4.3)."""

import asyncio
import contextlib
import itertools
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support.fake_ssh import FakeWatchSsh
from twinraid.integrations.ssh import SshTarget
from twinraid.integrations.vm_watch import (
    RetryPlan,
    VmWatch,
    WatchLineError,
    WatchReport,
    WatchTimings,
    WatchView,
    parse_line,
    watch_outcome,
)
from twinraid.shared.actions import ActionOutcome
from twinraid.shared.status import StatusReason, WatchRetry

GAMING = "Windows 11 Gaming"


def hex_of(text: str) -> str:
    return text.encode().hex()


def line(
    vm: str = "SHUTOFF",
    name: str | None = GAMING,
    others: str = "0",
    names: list[str] | None = None,
    connect: str = "absent",
    flash: str = "inactive",
    version: int = 2,
) -> str:
    encoded = ",".join(hex_of(item) for item in names) if names else "-"
    own = hex_of(name) if name is not None else "-"
    return (
        f"watch {version} vm={vm} name={own} others={others} names={encoded} "
        f"connect={connect} flash_backup={flash}"
    )


# --- Zeilenformat --------------------------------------------------------------------------


def test_parse_a_line() -> None:
    report = parse_line((line("RUNNING", others="2", names=["B-VM", "A VM"]) + "\n").encode())
    assert report == WatchReport(
        vm="RUNNING",
        name=GAMING,
        others=("A VM", "B-VM"),
        connect_plugin=False,
        flash_backup="inactive",
    )


@pytest.mark.parametrize(
    "vm",
    [
        "RUNNING", "IDLE", "PAUSED", "SHUTDOWN", "SHUTOFF", "CRASHED", "PMSUSPENDED", "NOSTATE",
        "NOT_CONFIGURED", "NOT_FOUND", "LIBVIRT_DOWN", "UNKNOWN",
    ],
)  # fmt: skip
def test_every_state_is_accepted(vm: str) -> None:
    assert parse_line(line(vm).encode()).vm == vm


def test_unknown_list_and_markers() -> None:
    report = parse_line(line("LIBVIRT_DOWN", name=None, others="?", connect="present").encode())
    assert report.others is None
    assert report.name is None
    assert report.connect_plugin is True
    assert parse_line(line(flash="active").encode()).flash_backup == "active"


def test_names_are_decoded_and_cleaned() -> None:
    names = ["Größe", "Steuer\x1bzeichen", "x" * 80]
    report = parse_line(line(others="3", names=names).encode())
    assert report.others == ("Größe", "Steuer?zeichen", "x" * 63 + "…")


def test_at_most_twenty_names_for_more_vms() -> None:
    names = [f"VM {index:02d}" for index in range(20)]
    report = parse_line(line(others="25", names=names).encode())
    assert report.others is not None
    assert len(report.others) == 20


def test_broken_utf8_is_replaced() -> None:
    text = line(others="1").replace("names=-", "names=ff41")
    assert parse_line(text.encode()).others == ("�A",)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "garbage",
        line() + " ",
        " " + line(),
        line() + " extra=1",
        line("RUNNING").replace("vm=RUNNING", "vm=running"),
        line("STARTING"),
        line().replace(f"name={hex_of(GAMING)}", "name=ABCD"),
        line().replace(hex_of(GAMING), hex_of(GAMING)[:-1]),
        line(others="1"),
        line(others="0", names=["A"]),
        line(others="2", names=["A"]),
        line(others="?", names=["A"]),
        line(others="21", names=[f"VM {index}" for index in range(21)]),
        line(connect="maybe"),
        line(flash="yes"),
        line().replace("watch 2", "watch x"),
        line().replace("watch", "status"),
        line() + "\nwatch 2",
    ],
)
def test_malformed_lines_are_rejected(text: str) -> None:
    with pytest.raises(WatchLineError) as excinfo:
        parse_line(text.encode())
    assert excinfo.value.outdated is False


def test_non_ascii_is_rejected() -> None:
    with pytest.raises(WatchLineError):
        parse_line(line().replace("absent", "ábsent").encode())


@pytest.mark.parametrize("text", [line(version=1), "watch 1 vm=RUNNING old format"])
def test_old_guard_versions_are_outdated(text: str) -> None:
    with pytest.raises(WatchLineError) as excinfo:
        parse_line(text.encode())
    assert excinfo.value.outdated is True


def test_newer_guard_with_the_same_line_is_accepted() -> None:
    assert parse_line(line(version=3).encode()).vm == "SHUTOFF"


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [
        (StatusReason.SSH_AUTH_FAILED, ActionOutcome.SSH_AUTH_FAILED),
        (StatusReason.GUARD_OUTDATED, ActionOutcome.GUARD_OUTDATED),
        (StatusReason.WATCH_INTERRUPTED, ActionOutcome.WATCH_INTERRUPTED),
        (StatusReason.WATCH_CONNECTING, ActionOutcome.WATCH_INTERRUPTED),
        (None, ActionOutcome.WATCH_INTERRUPTED),
    ],
)
def test_watch_outcome(reason: StatusReason | None, outcome: ActionOutcome) -> None:
    assert watch_outcome(reason) is outcome


# --- Neuversuche (rein rechnerisch) -------------------------------------------------------------

PLAN_TIMINGS = WatchTimings()
FILES: tuple[None, ...] = (None, None)


def test_transient_delays() -> None:
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    now = 1000.0
    waits = []
    for _ in range(6):
        plan.transient(now, session_seconds=1, booting=False)
        waits.append(plan.wait_seconds(now))
        assert not plan.may_connect(now, FILES)
        now += waits[-1]
        assert plan.may_connect(now, FILES)
    assert waits == [5, 15, 60, 300, 300, 300]


def test_a_long_session_starts_the_delays_again() -> None:
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    for _ in range(3):
        plan.transient(0, session_seconds=1, booting=False)
    assert plan.wait_seconds(0) == 60
    plan.transient(0, session_seconds=61, booting=False)
    assert plan.wait_seconds(0) == 5
    plan.transient(0, session_seconds=59, booting=False)
    assert plan.wait_seconds(0) == 15


def test_boot_window_retries_every_three_seconds() -> None:
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    plan.came_online(1000)
    assert plan.in_boot_window(1299)
    for _ in range(10):
        plan.transient(1100, session_seconds=0, booting=True)
        assert plan.wait_seconds(1100) == 3
    # Nach dem Fenster gelten die normalen Wartezeiten, beginnend bei 5 s.
    assert not plan.in_boot_window(1300)
    plan.transient(1300, session_seconds=0, booting=True)
    assert plan.wait_seconds(1300) == 5
    # Andere Fehler im Fenster folgen ebenfalls den normalen Wartezeiten.
    plan.came_online(2000)
    plan.transient(2001, session_seconds=0, booting=False)
    assert plan.wait_seconds(2001) == 5


def test_no_boot_window_without_a_known_offline_phase() -> None:
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    assert not plan.in_boot_window(0)
    plan.transient(0, session_seconds=0, booting=True)
    assert plan.wait_seconds(0) == 5


def test_permanent_failure_pauses_for_thirty_minutes() -> None:
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    plan.permanent(0, FILES)
    assert plan.paused
    assert not plan.may_connect(1799, FILES)
    # Während der Pause wird alle 5 s geprüft, ob sich die Dateien geändert haben.
    assert plan.wait_seconds(0) == 5
    assert plan.may_connect(1800, FILES)


def test_changed_key_files_end_the_pause() -> None:
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    plan.permanent(0, ((1, 2, 3), None))
    assert not plan.may_connect(10, ((1, 2, 3), None))
    assert plan.may_connect(10, ((1, 2, 4), None))
    assert not plan.paused


def test_retry_now_ends_every_wait() -> None:
    """trust-host-key mit Erfolg (SIGUSR2): Pause und Wartezeit enden sofort (ab v0.4.4)."""
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    plan.permanent(0, FILES)
    assert plan.remaining(10) == 1790
    plan.retry_now()
    assert not plan.paused
    assert plan.remaining(10) == 0
    assert plan.may_connect(10, FILES)
    for _ in range(3):
        plan.transient(20, session_seconds=1, booting=False)
    assert plan.remaining(20) == 60
    plan.retry_now()
    assert plan.may_connect(20, FILES)
    # Die Folge der Wartezeiten beginnt danach von vorn.
    plan.transient(20, session_seconds=1, booting=False)
    assert plan.remaining(20) == 5


def test_offline_and_online_end_the_pause() -> None:
    plan = RetryPlan(PLAN_TIMINGS, boot_window_seconds=300)
    plan.permanent(0, FILES)
    plan.went_offline()
    assert plan.may_connect(1, FILES)
    plan.permanent(2, FILES)
    plan.came_online(3)
    assert plan.may_connect(3, FILES)


# --- Ablauf mit nachgebautem ssh ---------------------------------------------------------

TIMINGS = WatchTimings(
    stall_seconds=0.8,
    retry_delays=(0.3, 0.6, 1.2),
    stable_session_seconds=2.0,
    permanent_pause_seconds=60.0,
    boot_retry_seconds=0.05,
    file_poll_seconds=0.05,
)
pytestmark = pytest.mark.anyio


# Feste Uhrzeit für den nächsten Versuch im Status (ganze Sekunden).
CLOCK = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)


class Setup:
    def __init__(self, tmp_path: Path, timings: WatchTimings = TIMINGS) -> None:
        self.fake = FakeWatchSsh.install(tmp_path / "bin")
        self.key = tmp_path / "ssh-status"
        self.known = tmp_path / "known"
        self.key.write_text("key")
        self.known.write_text("x ssh-ed25519 AAAA\n")
        target = SshTarget("192.0.2.11", 22, self.key, self.known)
        self.watch = VmWatch(
            target,
            boot_window_seconds=5.0,
            timings=timings,
            binary=self.fake.binary,
            clock=lambda: CLOCK,
        )

    def view(self) -> WatchView:
        return self.watch.view()

    def report(self) -> WatchReport | None:
        return self.watch.view().report


async def until(predicate: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition not reached in time")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def running(setup: Setup) -> AsyncIterator[Setup]:
    task = asyncio.create_task(setup.watch.run())
    try:
        yield setup
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_connects_only_while_online(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs({"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        await asyncio.sleep(0.3)
        assert setup.fake.calls() == []
        assert setup.view() == WatchView(None, StatusReason.WATCH_CONNECTING)
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)
        report = setup.report()
        assert report is not None
        assert report.vm == "RUNNING"
        [call] = setup.fake.calls()
        assert call[call.index("-i") + 1] == str(setup.key)
        assert call[-3:] == ["--", "root@192.0.2.11", "vm-watch"]


async def test_going_offline_ends_the_session(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs({"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)
        setup.watch.set_target_online(False)
        assert setup.view() == WatchView(None, StatusReason.WATCH_CONNECTING)
        # Der ssh-Prozess wird sofort beendet, und es folgt kein neuer Versuch.
        await until(lambda: setup.watch._process is None, seconds=2)
        await asyncio.sleep(0.5)
        assert len(setup.fake.calls()) == 1
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)
        assert len(setup.fake.calls()) == 2


async def test_changes_are_signalled_but_keepalives_are_not(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    first, second = line("SHUTOFF"), line("RUNNING")
    setup.fake.runs({"lines": [[0, first], [0.2, first], [0.4, second]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)
        setup.watch.changed.clear()
        await asyncio.sleep(0.35)
        assert not setup.watch.changed.is_set()
        await until(lambda: (r := setup.report()) is not None and r.vm == "RUNNING")
        assert setup.watch.changed.is_set()


async def test_a_stalled_session_is_replaced(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs(
        {"lines": [[0, line("SHUTOFF")]], "hold": 60},
        {"lines": [[0, line("RUNNING")]], "hold": 60},
    )
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)
        started = time.monotonic()
        await until(lambda: setup.view().reason is StatusReason.WATCH_INTERRUPTED, seconds=3)
        assert time.monotonic() - started >= 0.6
        await until(lambda: (r := setup.report()) is not None and r.vm == "RUNNING")
        assert len(setup.fake.calls()) == 2


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        ({"exit": 255, "stderr": "root@x: Permission denied (publickey).\n"}, "ssh_auth_failed"),
        ({"exit": 255, "stderr": "Host key verification failed.\n"}, "ssh_host_key_mismatch"),
        ({"exit": 2, "stderr": "twinraid-guard: denied\n"}, "ssh_denied"),
        ({"lines": [[0, line(version=1)]], "hold": 60}, "guard_outdated"),
    ],
)
async def test_permanent_failures_wait_for_a_change(
    tmp_path: Path, run: dict[str, object], reason: str
) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs(run, {"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason(reason))
        await asyncio.sleep(0.6)
        assert len(setup.fake.calls()) == 1
        # Ein neuer Key (init-ssh-keys) beendet die Pause ohne Neustart.
        setup.key.write_text("new key")
        await until(lambda: setup.report() is not None)
        assert len(setup.fake.calls()) == 2


async def test_the_status_names_the_next_attempt(tmp_path: Path) -> None:
    """Nach einem dauerhaften Fehler: Pause mit Zeitpunkt, aufgerundet (ab v0.4.4)."""
    setup = Setup(tmp_path)
    setup.fake.runs({"exit": 255, "stderr": "root@x: Permission denied (publickey).\n"})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.SSH_AUTH_FAILED)
        # permanent_pause_seconds ist im Test 60 s.
        assert setup.view() == WatchView(
            None,
            StatusReason.SSH_AUTH_FAILED,
            WatchRetry(at=CLOCK + timedelta(seconds=60), paused=True),
        )


async def test_transient_failures_name_the_next_attempt(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    refused = {"exit": 255, "stderr": "ssh: connect to host x port 22: Connection refused\n"}
    setup.fake.runs(refused, {"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.SSH_UNREACHABLE)
        # Erste Wartezeit 0,3 s, auf die volle Sekunde aufgerundet; keine Pause.
        assert setup.view().retry == WatchRetry(at=CLOCK + timedelta(seconds=1), paused=False)
        await until(lambda: setup.report() is not None)
        # Mit einem Stand gibt es keinen geplanten Versuch.
        assert setup.view().retry is None


async def test_retry_now_ends_the_pause_without_a_changed_file(tmp_path: Path) -> None:
    """Der gemeldete Fall: Zeile auf Groß nachgetragen, trust-host-key bestanden (ab v0.4.4).

    Keine Datei in TwinRaid ändert sich; ohne SIGUSR2 wartete die Sitzung bis zu 30 Minuten.
    """
    setup = Setup(tmp_path)
    setup.fake.runs(
        {"exit": 255, "stderr": "root@x: Permission denied (publickey).\n"},
        {"lines": [[0, line("RUNNING")]], "hold": 60},
    )
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.SSH_AUTH_FAILED)
        await asyncio.sleep(0.5)
        assert len(setup.fake.calls()) == 1
        started = time.monotonic()
        setup.watch.retry_now()
        await until(lambda: setup.report() is not None)
        assert time.monotonic() - started < 2
        assert len(setup.fake.calls()) == 2
        assert setup.view().retry is None


async def test_retry_now_leaves_a_running_session_alone(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs({"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)
        setup.watch.retry_now()
        await asyncio.sleep(0.5)
        assert len(setup.fake.calls()) == 1
        assert setup.report() is not None


async def test_retry_now_does_not_connect_while_offline(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs({"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.retry_now()
        await asyncio.sleep(0.3)
        assert setup.fake.calls() == []
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)


async def test_a_restart_of_the_target_ends_the_pause(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs(
        {"exit": 255, "stderr": "Permission denied (publickey).\n"},
        {"lines": [[0, line("RUNNING")]], "hold": 60},
    )
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.SSH_AUTH_FAILED)
        setup.watch.set_target_online(False)
        setup.watch.set_target_online(True)
        await until(lambda: setup.report() is not None)


async def test_the_pause_ends_after_its_time(tmp_path: Path) -> None:
    timings = WatchTimings(
        stall_seconds=0.8,
        retry_delays=(0.3,),
        permanent_pause_seconds=0.5,
        file_poll_seconds=0.05,
    )
    setup = Setup(tmp_path, timings)
    setup.fake.runs(
        {"exit": 2, "stderr": "twinraid-guard: denied\n"},
        {"lines": [[0, line("RUNNING")]], "hold": 60},
    )
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.SSH_DENIED)
        await until(lambda: setup.report() is not None)
        times = setup.fake.call_times()
        assert times[1] - times[0] >= 0.45


@pytest.mark.parametrize(
    ("missing", "reason"),
    [("key", StatusReason.SSH_KEY_MISSING), ("known", StatusReason.SSH_HOST_UNTRUSTED)],
)
async def test_missing_files_start_no_ssh(
    tmp_path: Path, missing: str, reason: StatusReason
) -> None:
    setup = Setup(tmp_path)
    path = setup.key if missing == "key" else setup.known
    path.unlink()
    setup.fake.runs({"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is reason)
        await asyncio.sleep(0.3)
        assert setup.fake.calls() == []
        path.write_text("x ssh-ed25519 AAAA\n")
        await until(lambda: setup.report() is not None)


async def test_transient_failures_are_retried_with_growing_delays(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    refused = {"exit": 255, "stderr": "ssh: connect to host x port 22: Connection refused\n"}
    setup.fake.runs(refused, refused, refused, {"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.SSH_UNREACHABLE)
        await until(lambda: setup.report() is not None, seconds=8)
        times = setup.fake.call_times()
        gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
        assert gaps[0] >= 0.25
        assert gaps[1] >= 0.55
        assert gaps[2] >= 1.1


@pytest.mark.parametrize(
    "run",
    [
        {"exit": 3, "stderr": "twinraid-guard: refused watch-busy\n"},
        {"lines": [[0, "garbage"]], "hold": 60},
        {"lines": [[0, line()]], "exit": 0},
        {"lines": [[0, "x" * 20000]], "hold": 60},
    ],
)
async def test_interruptions_are_retried(tmp_path: Path, run: dict[str, object]) -> None:
    setup = Setup(tmp_path)
    setup.fake.runs(run, {"lines": [[0, line("RUNNING")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.WATCH_INTERRUPTED)
        await until(lambda: (r := setup.report()) is not None and r.vm == "RUNNING")


async def test_after_a_boot_sshd_is_retried_quickly(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    refused = {"exit": 255, "stderr": "ssh: connect to host x port 22: Connection refused\n"}
    setup.fake.runs(refused, refused, refused, {"lines": [[0, line("LIBVIRT_DOWN")]], "hold": 60})
    async with running(setup):
        setup.watch.set_target_online(False)
        setup.watch.set_target_online(True)
        await until(lambda: len(setup.fake.calls()) >= 1)
        # Kein Fehler in der Anzeige, solange Groß gerade startet, und kein geplanter
        # Versuch: Die Sitzung versucht es alle 3 s (ab v0.4.4).
        assert setup.view().reason in (StatusReason.WATCH_CONNECTING, None)
        assert setup.view().retry is None
        started = time.monotonic()
        await until(lambda: setup.report() is not None)
        assert time.monotonic() - started < 2
        assert len(setup.fake.calls()) == 4


async def test_vm_names_are_never_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    setup = Setup(tmp_path)
    secret = "Geheime VM"
    setup.fake.runs({"lines": [[0, line("RUNNING", others="1", names=[secret])]], "exit": 0})
    caplog.set_level("DEBUG")
    async with running(setup):
        setup.watch.set_target_online(True)
        await until(lambda: setup.view().reason is StatusReason.WATCH_INTERRUPTED)
    assert "vm-watch: session started" in caplog.text
    assert "vm-watch: session ended (watch_interrupted)" in caplog.text
    assert secret not in caplog.text
    assert hex_of(secret) not in caplog.text
