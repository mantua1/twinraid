"""Echter Sync im Worker (Konzept 4.3, „Sync in TwinRaid“, ab v0.5.2)."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.scripted_guard import Prepared, ScriptedGuard, Sent, ok_check
from tests.support.worker import TestWorker, make_worker
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.mocks.lab import LabTimings
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome
from twinraid.shared.mode import RunMode
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardConfirmReason,
    GuardContainer,
    GuardContainerState,
    GuardDatasetResult,
    GuardJob,
    GuardJobReason,
    GuardJobResult,
    GuardJobState,
    GuardOrder,
    GuardOrderResult,
    GuardRestart,
)
from twinraid.shared.status import ResourceState
from twinraid.shared.sync import SyncKind, SyncStep
from twinraid.sync.coordinator import SyncTimings
from twinraid.worker.actions import ActionTimings
from twinraid.worker.sync_history import read_history

pytestmark = pytest.mark.anyio

CONFIG = load_config(EXAMPLE_CONFIG)
FAST_LAB = LabTimings(
    boot_seconds=0.05,
    vm_service_seconds=0.05,
    vm_start_seconds=0.01,
    vm_stop_seconds=0.01,
    shutdown_seconds=0.01,
)
FAST_SYNC = SyncTimings(job_poll_seconds=0.01, startup_poll_seconds=0.01, ping_seconds=0.05)
RUNNING = GuardContainerState.RUNNING
# Wartezeit auf stop: stop_timeout_seconds des Wächters (60) plus 15 s (Konzept 3.6).
STOP_REPLY = 75.0


def _config(**changes: Any) -> TwinRaidConfig:  # noqa: ANN401 - Werte der Konfiguration
    sync = CONFIG.sync.model_copy(update=changes)
    return CONFIG.model_copy(update={"sync": sync})


def _containers(*names: str, state: GuardContainerState = RUNNING) -> tuple[GuardContainer, ...]:
    return tuple(GuardContainer(name=name, state=state) for name in names)


def _guard(**values: Any) -> ScriptedGuard:  # noqa: ANN401 - Felder des Drehbuchs
    values.setdefault("allowed", _containers("web", "db"))
    return ScriptedGuard(**values)


def _worker(
    tmp_path: Path,
    guard: ScriptedGuard,
    config: TwinRaidConfig = CONFIG,
    mode: RunMode = RunMode.MOCK,
    sync_timings: SyncTimings = FAST_SYNC,
) -> TestWorker:
    return make_worker(
        tmp_path,
        mode,
        config=config,
        source_guard=guard,
        channel=guard,
        action_timings=ActionTimings(poll_seconds=0.01),
        lab_timings=FAST_LAB,
        sync_timings=sync_timings,
    )


async def _online(worker: TestWorker) -> None:
    await worker.lab.wake()
    for _ in range(200):
        status = await worker.monitor.check_once()
        if status.target.state is ResourceState.ONLINE and status.vm.reason is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("simulated target did not come up")


async def _until(predicate: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def _done(worker: TestWorker) -> None:
    await _until(lambda: worker.sync.snapshot().running is None)


async def _start(worker: TestWorker) -> str:
    assert await worker.actions.request(ActionName.SYNC_START) is None
    running = worker.sync.snapshot().running
    assert running is not None
    assert running.kind is SyncKind.SYNC
    return running.job_id


async def _sync(worker: TestWorker) -> None:
    await _start(worker)
    await _done(worker)


def _outcome(worker: TestWorker) -> tuple[ActionOutcome, object]:
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.kind is SyncKind.SYNC
    return last.outcome, last.reason


def _log(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "actions.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _main_orders(guard: ScriptedGuard) -> list[GuardOrder]:
    """Aufträge ohne die ping des Taktgebers."""
    return [order for order in guard.orders if order is not GuardOrder.PING]


def _step(worker: TestWorker) -> SyncStep | None:
    running = worker.sync.snapshot().running
    return running.step if running is not None else None


def _waiting(worker: TestWorker) -> tuple[bool, bool]:
    """Abbruch angefordert, wartet auf den Container (Konzept 4.3, „Status“)."""
    running = worker.sync.snapshot().running
    assert running is not None
    return running.cancelling, running.waiting_for_container


# --- Verfügbarkeit ---------------------------------------------------------------------------


async def test_sync_is_available(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _guard())
    await _online(worker)
    assert worker.actions.availability(worker.monitor.current())[ActionName.SYNC_START] is None


@pytest.mark.parametrize(
    ("values", "config", "blocker"),
    [
        # Pause zwischen zwei Läufen, nur mit stop_containers (Konzept 4.2).
        (
            {"run_allowed_at": datetime.now(UTC) + timedelta(minutes=5)},
            CONFIG,
            ActionBlocker.SOURCE_GUARD_TOO_SOON,
        ),
        (
            {"run_allowed_at": datetime.now(UTC) + timedelta(minutes=5)},
            _config(stop_containers=False),
            None,
        ),
        ({"run_allowed_at": datetime.now(UTC) - timedelta(minutes=1)}, CONFIG, None),
        # Spiegel aus dem letzten check als dritte Ebene; ohne check sperrt sie nicht.
        ({"check": ok_check(("appdata", "dokumente"))}, CONFIG, ActionBlocker.LEVELS_MISMATCH),
        ({"check": ok_check()}, CONFIG, None),
        ({"check": None}, CONFIG, None),
        ({"check": ok_check(clones=1)}, CONFIG, ActionBlocker.TARGET_CLONE_ACTIVE),
        ({"check": ok_check(clones=None)}, CONFIG, None),
    ],
)
async def test_sync_blockers(
    tmp_path: Path,
    values: dict[str, Any],
    config: TwinRaidConfig,
    blocker: ActionBlocker | None,
) -> None:
    worker = _worker(tmp_path, _guard(**values), config)
    await _online(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is blocker
    # Der Probelauf ist von diesen Sperren nicht betroffen (Konzept 4.3).
    assert availability[ActionName.SYNC_DRY_RUN] is None


async def test_one_run_at_a_time(tmp_path: Path) -> None:
    guard = _guard(hold=GuardOrder.SNAPSHOT)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.BUSY
    assert availability[ActionName.SYNC_DRY_RUN] is ActionBlocker.BUSY
    assert availability[ActionName.SHUTDOWN_TARGET] is ActionBlocker.SYNC_RUNNING
    assert worker.sync.is_running()
    guard.release.set()
    await _done(worker)


# --- Ablauf ---------------------------------------------------------------------------------


async def test_successful_sync(tmp_path: Path) -> None:
    guard = _guard(
        allowed=(*_containers("web", "db"), *_containers("old", state=GuardContainerState.STOPPED))
    )
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    # begin, je Kandidat ping und stop, ein letztes ping, snapshot, finish, prepare, send.
    stops = [(c, r) for order, c, r in guard.calls if order is GuardOrder.STOP]
    assert stops == [("web", STOP_REPLY), ("db", STOP_REPLY)]
    assert _main_orders(guard) == [
        GuardOrder.BEGIN,
        GuardOrder.STOP,
        GuardOrder.STOP,
        GuardOrder.SNAPSHOT,
        GuardOrder.FINISH,
        GuardOrder.PREPARE,
        GuardOrder.SEND,
    ]
    assert guard.orders[:6] == [
        GuardOrder.BEGIN,
        GuardOrder.PING,
        GuardOrder.STOP,
        GuardOrder.PING,
        GuardOrder.STOP,
        GuardOrder.PING,
    ]
    # Die Container laufen wieder; der Job ist fertig.
    assert {item.state for item in guard.allowed if item.name != "old"} == {RUNNING}
    assert guard.job is not None
    assert guard.job.state is GuardJobState.DONE
    status = worker.sync.snapshot()
    assert status.report is not None
    assert status.report.kind is SyncKind.SYNC
    assert {share.result for share in status.report.shares} == {GuardDatasetResult.OK}
    assert not worker.sync.is_running()


async def test_action_log_and_history(tmp_path: Path) -> None:
    guard = _guard()
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    entries = [
        (e["action"], e["trigger"], e.get("part_of"), e["result"], e.get("containers"))
        for e in _log(tmp_path)
    ]
    assert entries == [
        ("sync_start", "dashboard", None, "started", None),
        ("source_guard_stop", "automatic", "sync_start", "started", None),
        ("source_guard_stop", "automatic", "sync_start", "success", 2),
        ("source_guard_finish", "automatic", "sync_start", "started", None),
        ("source_guard_finish", "automatic", "sync_start", "success", None),
        ("sync_start", "dashboard", None, "success", None),
    ]
    # Keine Container-Namen im Aktionslog (Konzept 5.3).
    assert "web" not in (tmp_path / "actions.jsonl").read_text()
    history = read_history(tmp_path / "sync-history.jsonl", 10)
    assert len(history) == 1
    entry = history[0]
    assert (entry.kind, entry.result, entry.containers_stopped, entry.adopted) == (
        SyncKind.SYNC,
        ActionOutcome.SUCCESS,
        2,
        False,
    )
    assert entry.woke_target is False
    assert [share.name for share in entry.shares] == ["appdata", "dokumente", "medien"]
    assert {share.result for share in entry.shares} == {GuardDatasetResult.OK}
    text = (tmp_path / "sync-history.jsonl").read_text()
    assert "twinraid-2026" not in text
    assert "web" not in text
    assert (tmp_path / "sync-history.jsonl").stat().st_mode & 0o777 == 0o600
    assert worker.sync.snapshot().history == (entry,)


async def test_dry_runs_are_in_the_history(tmp_path: Path) -> None:
    guard = _guard()
    worker = _worker(tmp_path, guard)
    await _online(worker)
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    await _done(worker)
    # Wie nach 31 Minuten: Der Abstand zwischen Snapshots ist vorbei (ab v0.5.3).
    assert guard.job is not None
    guard.job = guard.job.model_copy(
        update={"created_at": guard.job.created_at - timedelta(minutes=31)}
    )
    await _sync(worker)
    history = worker.sync.snapshot().history
    # Neueste zuerst.
    assert [entry.kind for entry in history] == [SyncKind.SYNC, SyncKind.DRY_RUN]
    assert history[1].result is ActionOutcome.DRY_RUN_SUCCESS


async def test_sync_wakes_the_target(tmp_path: Path) -> None:
    guard = _guard()
    worker = _worker(tmp_path, guard)
    await worker.monitor.check_once()
    await _start(worker)
    assert _step(worker) is SyncStep.WAKING
    await _until(lambda: worker.sync.snapshot().running is None, 10)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    entries = [(e["action"], e["result"]) for e in _log(tmp_path)]
    assert ("wake_target", "success") in entries
    history = worker.sync.snapshot().history
    assert history[0].woke_target is True


async def test_without_stopping_containers(tmp_path: Path) -> None:
    guard = _guard()
    worker = _worker(tmp_path, guard, _config(stop_containers=False))
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    assert guard.orders == [GuardOrder.SNAPSHOT, GuardOrder.PREPARE, GuardOrder.SEND]


async def test_candidates(tmp_path: Path) -> None:
    """Nur laufende Container, nicht never_stop, nie TwinRaid selbst (Konzept 4.3)."""
    guard = _guard(
        allowed=(
            *_containers("web", "db", "cache", "self"),
            *_containers("off", state=GuardContainerState.STOPPED),
            *_containers("gone", state=GuardContainerState.MISSING),
        ),
        own=frozenset({"self"}),
    )
    worker = _worker(tmp_path, guard, _config(never_stop=["cache"]))
    await _online(worker)
    await _sync(worker)
    stops = [c for order, c, _r in guard.calls if order is GuardOrder.STOP]
    assert stops == ["web", "db"]


async def test_not_running_continues(tmp_path: Path) -> None:
    guard = _guard(answers={GuardOrder.STOP: [GuardOrderResult.NOT_RUNNING]})
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    assert worker.sync.snapshot().history[0].containers_stopped == 1


@pytest.mark.parametrize(
    "result",
    [
        GuardOrderResult.STOP_FAILED,
        GuardOrderResult.NOT_ALLOWED,
        GuardOrderResult.NOT_FOUND,
        GuardOrderResult.DENIED_SELF,
        GuardOrderResult.DOCKER_UNAVAILABLE,
        GuardOrderResult.NO_RUN,
    ],
)
async def test_stop_failure_ends_before_the_snapshot(
    tmp_path: Path, result: GuardOrderResult
) -> None:
    guard = _guard(answers={GuardOrder.STOP: [GuardOrderResult.STOPPED, result]})
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (ActionOutcome.SOURCE_GUARD_REFUSED, result)
    orders = _main_orders(guard)
    assert GuardOrder.SNAPSHOT not in orders
    # finish folgt immer und startet den schon angehaltenen Container wieder.
    assert orders[-1] is GuardOrder.FINISH
    stop = [e for e in _log(tmp_path) if e["action"] == "source_guard_stop"][-1]
    assert (stop["result"], stop["reason"], stop["containers"]) == (
        "source_guard_refused",
        result.value,
        1,
    )


async def test_begin_refused(tmp_path: Path) -> None:
    guard = _guard(answers={GuardOrder.BEGIN: [GuardOrderResult.TOO_SOON]})
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (ActionOutcome.SOURCE_GUARD_REFUSED, GuardOrderResult.TOO_SOON)
    assert guard.orders == [GuardOrder.BEGIN]


async def test_deadman_ends_before_the_snapshot(tmp_path: Path) -> None:
    """ping antwortet no_run: Der Totmannschalter hat den Lauf geschlossen (Konzept 4.3)."""
    guard = _guard(answers={GuardOrder.PING: [GuardOrderResult.EXTENDED, GuardOrderResult.NO_RUN]})
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (ActionOutcome.SOURCE_GUARD_REFUSED, GuardOrderResult.NO_RUN)
    assert GuardOrder.SNAPSHOT not in guard.orders


async def test_pings_while_the_run_is_open(tmp_path: Path) -> None:
    guard = _guard(hold=GuardOrder.SNAPSHOT)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: GuardOrder.SNAPSHOT in guard.orders)
    before = guard.orders.count(GuardOrder.PING)
    await _until(lambda: guard.orders.count(GuardOrder.PING) >= before + 2)
    guard.release.set()
    await _done(worker)
    # Nach finish kein ping mehr.
    finish = guard.orders.index(GuardOrder.FINISH)
    assert GuardOrder.PING not in guard.orders[finish:]


@pytest.mark.parametrize(
    ("restart", "sent", "outcome", "reason"),
    [
        (GuardRestart.START_FAILED, Sent(), ActionOutcome.CONTAINER_RESTART_FAILED, None),
        (GuardRestart.CHANGED, Sent(), ActionOutcome.CONTAINER_RESTART_FAILED, None),
        (GuardRestart.GONE, Sent(), ActionOutcome.CONTAINER_RESTART_FAILED, None),
        # Ein Fehler beim Senden geht vor.
        (
            GuardRestart.START_FAILED,
            Sent(result=GuardJobResult.SEND_FAILED, reason=GuardJobReason.UNREACHABLE),
            ActionOutcome.SEND_FAILED,
            GuardJobReason.UNREACHABLE,
        ),
    ],
)
async def test_restart_failure(
    tmp_path: Path,
    restart: GuardRestart,
    sent: Sent,
    outcome: ActionOutcome,
    reason: GuardJobReason | None,
) -> None:
    guard = _guard(restarts={"db": restart}, sent=sent)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    # Gesendet wird trotzdem, weil die Daten im Snapshot liegen (Konzept 4.3).
    assert GuardOrder.SEND in guard.orders
    assert _outcome(worker) == (outcome, reason)
    finish = [e for e in _log(tmp_path) if e["action"] == "source_guard_finish"][-1]
    assert finish["result"] == "container_restart_failed"


@pytest.mark.parametrize(
    ("prepared", "outcome", "reason"),
    [
        (
            Prepared(send_ready=False, blockers=(GuardBlocker.SPACE_LOW,)),
            ActionOutcome.TARGET_REFUSED,
            GuardBlocker.SPACE_LOW,
        ),
        (
            Prepared(send_ready=False, blockers=(GuardBlocker.BASE_MISSING,)),
            ActionOutcome.BASE_MISSING,
            None,
        ),
    ],
)
async def test_not_ready_to_send(
    tmp_path: Path, prepared: Prepared, outcome: ActionOutcome, reason: object
) -> None:
    guard = _guard(prepared=prepared)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (outcome, reason)
    assert _main_orders(guard)[-2:] == [GuardOrder.PREPARE, GuardOrder.CLOSE]
    assert GuardOrder.SEND not in guard.orders


@pytest.mark.parametrize(
    ("sent", "outcome", "reason"),
    [
        (
            Sent(result=GuardJobResult.SEND_FAILED, reason=GuardJobReason.UNREACHABLE),
            ActionOutcome.SEND_FAILED,
            GuardJobReason.UNREACHABLE,
        ),
        (
            Sent(result=GuardJobResult.SEND_FAILED, reason=GuardJobReason.SEND_ERROR),
            ActionOutcome.SEND_FAILED,
            GuardJobReason.SEND_ERROR,
        ),
        (
            Sent(result=GuardJobResult.SEND_FAILED, reason=GuardJobReason.BASE_MISSING),
            ActionOutcome.BASE_MISSING,
            None,
        ),
        (
            Sent(result=GuardJobResult.SEND_FAILED, reason=GuardJobReason.SELF_DATASET_MISSING),
            ActionOutcome.SELF_DATASET_MISSING,
            None,
        ),
        (
            Sent(result=GuardJobResult.TARGET_REFUSED, reason=GuardJobReason.SPACE_LOW),
            ActionOutcome.TARGET_REFUSED,
            GuardJobReason.SPACE_LOW,
        ),
        (
            Sent(result=GuardJobResult.TARGET_REFUSED, reason=GuardJobReason.REFUSED_OTHER),
            ActionOutcome.TARGET_REFUSED,
            GuardJobReason.REFUSED_OTHER,
        ),
        (Sent(result=GuardJobResult.TARGET_MODIFIED), ActionOutcome.TARGET_MODIFIED, None),
        (Sent(result=GuardJobResult.INTERRUPTED), ActionOutcome.SOURCE_GUARD_LOST, None),
        (Sent(result=GuardJobResult.JOB_TIMEOUT), ActionOutcome.JOB_TIMEOUT, None),
    ],
)
async def test_result_of_the_transfer(
    tmp_path: Path, sent: Sent, outcome: ActionOutcome, reason: GuardJobReason | None
) -> None:
    guard = _guard(sent=sent)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (outcome, reason)
    last = _log(tmp_path)[-1]
    assert last["result"] == outcome.value
    assert last.get("reason") == (reason.value if reason is not None else None)
    # Der Job ist zu Ende; ein close ist nicht mehr nötig.
    assert guard.orders[-1] is GuardOrder.SEND


async def test_progress_while_sending(tmp_path: Path) -> None:
    guard = _guard(sent=Sent(delay=200))
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: _step(worker) is SyncStep.SENDING)
    await _until(
        lambda: (
            (running := worker.sync.snapshot().running) is not None
            and running.progress is not None
            and running.progress.sent > 0
        )
    )
    running = worker.sync.snapshot().running
    assert running is not None
    progress = running.progress
    assert progress is not None
    assert progress.total == 3000
    assert progress.share == "appdata"
    assert 0 < progress.percent <= 99
    assert worker.sync.is_running()
    worker.sync.cancel(running.job_id)
    await _done(worker)


# --- Freigabe von Löschungen ----------------------------------------------------------------


def _awaiting() -> Prepared:
    return Prepared(
        state=GuardJobState.AWAITING_CONFIRM,
        confirm_reasons=(GuardConfirmReason.DELETIONS, GuardConfirmReason.SHRUNK),
    )


async def test_confirmation(tmp_path: Path) -> None:
    guard = _guard(prepared=_awaiting())
    worker = _worker(tmp_path, guard)
    await _online(worker)
    job_id = await _start(worker)
    await _until(lambda: _step(worker) is SyncStep.AWAITING_CONFIRM)
    running = worker.sync.snapshot().running
    assert running is not None
    confirmation = running.confirmation
    assert confirmation is not None
    assert confirmation.reasons == (GuardConfirmReason.DELETIONS, GuardConfirmReason.SHRUNK)
    # Je Share 1 Löschung im Drehbuch; Grenze aus dem Status des Wächters.
    assert (confirmation.deletions, confirmation.max_deletions) == (3, 1000)
    assert GuardOrder.SEND not in guard.orders
    assert worker.sync.confirm("0" * 32) is False
    assert worker.sync.confirm(job_id) is True
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    assert _main_orders(guard)[-3:] == [GuardOrder.PREPARE, GuardOrder.CONFIRM, GuardOrder.SEND]
    assert worker.sync.confirm(job_id) is False


async def test_declining_closes_the_job(tmp_path: Path) -> None:
    guard = _guard(prepared=_awaiting())
    worker = _worker(tmp_path, guard)
    await _online(worker)
    job_id = await _start(worker)
    await _until(lambda: _step(worker) is SyncStep.AWAITING_CONFIRM)
    assert worker.sync.cancel(job_id) is True
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.DELETIONS_DECLINED, None)
    assert _main_orders(guard)[-1] is GuardOrder.CLOSE
    assert GuardOrder.CONFIRM not in guard.orders


async def test_confirmation_timeout_of_the_guard(tmp_path: Path) -> None:
    """Der Wächter schließt den Job nach 30 min (Konzept 4.3, Schritt 5)."""

    def expire(guard: ScriptedGuard) -> None:
        job = guard.job
        if job is not None and job.state is GuardJobState.AWAITING_CONFIRM:
            guard.job = job.model_copy(
                update={"state": GuardJobState.CLOSED, "result": GuardJobResult.JOB_TIMEOUT}
            )

    guard = _guard(prepared=_awaiting())
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: _step(worker) is SyncStep.AWAITING_CONFIRM)
    guard.on_observe = expire
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.CONFIRMATION_TIMEOUT, None)


async def test_confirmation_timeout_of_the_worker(tmp_path: Path) -> None:
    guard = _guard(prepared=_awaiting())
    timings = SyncTimings(job_poll_seconds=0.01, confirm_wait_seconds=0.05, ping_seconds=0.05)
    worker = _worker(tmp_path, guard, sync_timings=timings)
    await _online(worker)
    await _sync(worker)
    assert _outcome(worker) == (ActionOutcome.CONFIRMATION_TIMEOUT, None)
    assert _main_orders(guard)[-1] is GuardOrder.CLOSE


# --- Abbrechen ------------------------------------------------------------------------------


async def test_cancel_while_sending(tmp_path: Path) -> None:
    guard = _guard(sent=Sent(delay=10_000))
    worker = _worker(tmp_path, guard)
    await _online(worker)
    job_id = await _start(worker)
    await _until(lambda: _step(worker) is SyncStep.SENDING and GuardOrder.SEND in guard.orders)
    assert worker.sync.cancel(job_id) is True
    # Ohne laufendes begin, stop oder finish wartet der Abbruch nicht auf den Container.
    assert _waiting(worker) == (True, False)
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.CANCELLED, None)
    # close bricht die Übertragung ab; auf Groß bleibt der alte Stand (Konzept 4.3).
    assert guard.orders[-1] is GuardOrder.CLOSE
    assert guard.job is not None
    assert guard.job.state is GuardJobState.CLOSED


async def test_cancel_while_stopping_waits_for_the_stop(tmp_path: Path) -> None:
    """Ein laufendes docker stop endet erst; dann startet finish den Container wieder."""
    guard = _guard(hold=GuardOrder.STOP)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    job_id = await _start(worker)
    await _until(lambda: GuardOrder.STOP in guard.orders)
    assert _waiting(worker) == (False, False)
    assert worker.sync.cancel(job_id) is True
    await asyncio.sleep(0.05)
    # Noch keine weitere Bestellung, solange stop läuft; die Kachel zeigt das (Konzept 4.3).
    assert _main_orders(guard) == [GuardOrder.BEGIN, GuardOrder.STOP]
    assert _waiting(worker) == (True, True)
    guard.release.set()
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.CANCELLED, None)
    assert _main_orders(guard) == [GuardOrder.BEGIN, GuardOrder.STOP, GuardOrder.FINISH]
    assert worker.sync.snapshot().history[0].containers_stopped == 1
    assert {item.state for item in guard.allowed} == {RUNNING}


async def test_cancel_during_the_snapshot_finishes_the_run(tmp_path: Path) -> None:
    guard = _guard(hold=GuardOrder.SNAPSHOT)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    job_id = await _start(worker)
    await _until(lambda: GuardOrder.SNAPSHOT in guard.orders)
    assert worker.sync.cancel(job_id) is True
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.CANCELLED, None)
    orders = _main_orders(guard)
    assert orders[-2:] == [GuardOrder.FINISH, GuardOrder.CLOSE]


# --- Ende und Start des Workers -------------------------------------------------------------


async def test_worker_end_while_containers_are_stopped(tmp_path: Path) -> None:
    """finish wird nur noch geschrieben; close folgt nicht, damit es finish nicht überschreibt."""
    guard = _guard(hold=GuardOrder.SNAPSHOT)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: GuardOrder.SNAPSHOT in guard.orders)
    await worker.close()
    assert guard.posted == [GuardOrder.FINISH]
    assert GuardOrder.FINISH not in guard.orders
    assert _log(tmp_path)[-1]["result"] == "interrupted"
    assert worker.sync.snapshot().history[0].result is ActionOutcome.INTERRUPTED


async def test_worker_end_while_stopping_does_not_wait(tmp_path: Path) -> None:
    guard = _guard(hold=GuardOrder.STOP)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: GuardOrder.STOP in guard.orders)
    await asyncio.wait_for(worker.close(), 2)
    assert guard.posted == [GuardOrder.FINISH]


async def test_worker_end_during_prepare_posts_close(tmp_path: Path) -> None:
    guard = _guard(prepared=None)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: GuardOrder.PREPARE in guard.orders)
    await worker.close()
    assert guard.posted == [GuardOrder.CLOSE]


async def test_worker_end_leaves_the_transfer(tmp_path: Path) -> None:
    guard = _guard(sent=Sent(delay=10_000))
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: GuardOrder.SEND in guard.orders)
    await worker.close()
    assert guard.posted == []
    assert guard.job is not None
    assert guard.job.state is GuardJobState.SENDING
    assert _log(tmp_path)[-1]["result"] == "interrupted"


def _sending_job() -> GuardJob:
    return GuardJob.model_validate(
        {
            "id": 7,
            "state": "sending",
            "result": None,
            "reason": None,
            "snapshot": "twinraid-20261105T120000Z",
            "created_at": "2026-11-05T12:00:00Z",
            "deadline": None,
            "confirmed": False,
            "confirm_reasons": [],
            "send_ready": True,
            "blockers": [],
            "datasets": [
                {
                    "name": name,
                    "mode": "incremental",
                    "base_type": "snapshot",
                    "problem": None,
                    "deleted": 1,
                    "modified": 2,
                    "added": 3,
                    "renamed": 0,
                    "referenced": 1000,
                    "base_referenced": 1000,
                    "size": 1000,
                    "sent": 0,
                    "result": None,
                }
                for name in ("appdata", "dokumente", "medien")
            ],
        }
    )


async def test_transfer_is_adopted_after_a_restart(tmp_path: Path) -> None:
    guard = _guard(job=_sending_job(), last_seq=7, sent=Sent(delay=3))
    worker = _worker(tmp_path, guard)
    await worker.sync.run()
    running = worker.sync.snapshot().running
    assert running is not None
    assert (running.kind, running.step, running.adopted) == (
        SyncKind.SYNC,
        SyncStep.SENDING,
        True,
    )
    assert worker.sync.is_running()
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    assert guard.orders == []
    entries = _log(tmp_path)
    # Ein einziger Eintrag am Ende, mit adopted (Konzept 4.3).
    assert [(e["action"], e["result"], e.get("adopted")) for e in entries] == [
        ("sync_start", "success", True)
    ]
    history = worker.sync.snapshot().history
    assert history[0].adopted is True
    assert history[0].started_at == datetime(2026, 11, 5, 12, tzinfo=UTC)


async def test_adopted_transfer_can_be_cancelled(tmp_path: Path) -> None:
    guard = _guard(job=_sending_job(), last_seq=7, sent=Sent(delay=10_000))
    worker = _worker(tmp_path, guard)
    await worker.sync.run()
    running = worker.sync.snapshot().running
    assert running is not None
    assert worker.sync.cancel(running.job_id) is True
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.CANCELLED, None)
    assert guard.orders == [GuardOrder.CLOSE]


async def test_open_run_is_finished_after_a_restart(tmp_path: Path) -> None:
    guard = _guard()
    await guard.order(GuardOrder.BEGIN)
    await guard.order(GuardOrder.STOP, "web")
    await guard.order(GuardOrder.SNAPSHOT)
    guard.orders.clear()
    worker = _worker(tmp_path, guard)
    await worker.sync.run()
    assert guard.orders == [GuardOrder.FINISH, GuardOrder.CLOSE]
    assert guard.run is None
    assert {item.state for item in guard.allowed} == {RUNNING}


async def test_worker_end_while_send_is_written(tmp_path: Path) -> None:
    """send ist geschrieben, die Antwort fehlt noch: kein close, die Übertragung beginnt."""
    guard = _guard(hold=GuardOrder.SEND)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _start(worker)
    await _until(lambda: GuardOrder.SEND in guard.orders)
    await worker.close()
    assert guard.posted == []


async def test_cancel_while_begin_is_open_finishes_the_run(tmp_path: Path) -> None:
    """Die Antwort auf begin kommt noch an; der offene Lauf wird mit finish geschlossen."""
    guard = _guard(hold=GuardOrder.BEGIN)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    job_id = await _start(worker)
    await _until(lambda: GuardOrder.BEGIN in guard.orders)
    assert worker.sync.cancel(job_id) is True
    assert _waiting(worker) == (True, True)
    guard.release.set()
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.CANCELLED, None)
    assert _main_orders(guard) == [GuardOrder.BEGIN, GuardOrder.FINISH]
    assert guard.run is None


async def test_cancel_while_finishing(tmp_path: Path) -> None:
    """finish läuft zu Ende und wird nicht wiederholt; danach gilt der Abbruch."""
    guard = _guard(hold=GuardOrder.FINISH)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    job_id = await _start(worker)
    await _until(lambda: _step(worker) is SyncStep.STARTING_CONTAINERS)
    await _until(lambda: GuardOrder.FINISH in guard.orders)
    assert worker.sync.cancel(job_id) is True
    assert _waiting(worker) == (True, True)
    guard.release.set()
    await _done(worker)
    assert _outcome(worker) == (ActionOutcome.CANCELLED, None)
    assert guard.orders.count(GuardOrder.FINISH) == 1
    # Der Job aus dem Snapshot wird geschlossen.
    assert _main_orders(guard)[-1] is GuardOrder.CLOSE
    assert {item.state for item in guard.allowed} == {RUNNING}
