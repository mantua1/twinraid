"""Probelauf im Worker (Konzept 4.3, „Probelauf in TwinRaid“, ab v0.5.1)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.scripted_guard import Prepared, ScriptedGuard, failed_check, ok_check
from tests.support.worker import TestWorker, make_worker
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.mocks.lab import LabTimings, fault_flag, set_flag, tour_flag
from twinraid.shared.actions import (
    ActionBlocker,
    ActionName,
    ActionOutcome,
    ActionStep,
)
from twinraid.shared.mode import RunMode
from twinraid.shared.source_guard import (
    GuardBlocker,
    GuardCheckResult,
    GuardConfirmReason,
    GuardError,
    GuardJob,
    GuardJobResult,
    GuardJobState,
    GuardOrder,
    GuardOrderResult,
    GuardSelfDataset,
    GuardState,
    GuardZfsState,
)
from twinraid.shared.status import ResourceState
from twinraid.shared.sync import SyncStep
from twinraid.sync.coordinator import SyncTimings, evaluate_prepare
from twinraid.worker.actions import ActionTimings

pytestmark = pytest.mark.anyio

CONFIG = load_config(EXAMPLE_CONFIG)
FAST_LAB = LabTimings(
    boot_seconds=0.05,
    vm_service_seconds=0.05,
    vm_start_seconds=0.01,
    vm_stop_seconds=0.01,
    shutdown_seconds=0.01,
)
FAST_ACTIONS = ActionTimings(poll_seconds=0.01)


def _config(**actions: bool) -> TwinRaidConfig:
    return CONFIG.model_copy(update={"actions": CONFIG.actions.model_copy(update=actions)})


def _worker(
    tmp_path: Path,
    guard: ScriptedGuard | None = None,
    mode: RunMode = RunMode.MOCK,
    config: TwinRaidConfig = CONFIG,
    lab_timings: LabTimings = FAST_LAB,
    sync_timings: SyncTimings | None = None,
) -> TestWorker:
    extra: dict[str, Any] = {}
    if sync_timings is not None:
        extra["sync_timings"] = sync_timings
    return make_worker(
        tmp_path,
        mode,
        config=config,
        source_guard=guard,
        channel=guard,
        action_timings=FAST_ACTIONS,
        lab_timings=lab_timings,
        **extra,
    )


async def _online(worker: TestWorker) -> None:
    """Groß an und der VM-Dienst bereit (Simulation)."""
    await worker.lab.wake()
    for _ in range(200):
        status = await worker.monitor.check_once()
        if status.target.state is ResourceState.ONLINE and status.vm.reason is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("simulated target did not come up")


async def _done(worker: TestWorker, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while worker.sync.snapshot().running is not None:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("dry run did not end")
        await asyncio.sleep(0.01)


def _blocker(worker: TestWorker, name: ActionName = ActionName.SYNC_DRY_RUN) -> object:
    return worker.actions.availability(worker.monitor.current())[name]


def _log(tmp_path: Path) -> list[dict[str, str]]:
    lines = (tmp_path / "actions.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


async def _run(worker: TestWorker) -> None:
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    await _done(worker)


# --- Verfügbarkeit ---------------------------------------------------------------------------


async def test_available_with_target_online(tmp_path: Path) -> None:
    worker = _worker(tmp_path, ScriptedGuard())
    await _online(worker)
    assert _blocker(worker) is None
    assert _blocker(worker, ActionName.SYNC_START) is ActionBlocker.NEXT_VERSION


async def test_real_mode_needs_a_switch(tmp_path: Path) -> None:
    worker = _worker(tmp_path, ScriptedGuard(), RunMode.REAL)
    await _online(worker)
    assert _blocker(worker) is ActionBlocker.NOT_ENABLED
    assert _blocker(worker, ActionName.SYNC_START) is ActionBlocker.NOT_ENABLED
    for switch in ("sync_dry_run", "sync"):
        (tmp_path / switch).mkdir()
        config = _config(**{switch: True})
        enabled = _worker(tmp_path / switch, ScriptedGuard(), RunMode.REAL, config)
        await _online(enabled)
        assert _blocker(enabled) is None
    # actions.sync schaltet auch den Sync frei; der kommt aber erst mit v0.5.2.
    (tmp_path / "start").mkdir()
    sync_on = _worker(tmp_path / "start", ScriptedGuard(), RunMode.REAL, _config(sync=True))
    assert _blocker(sync_on, ActionName.SYNC_START) is ActionBlocker.NEXT_VERSION


@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        ({"state": GuardState.INACTIVE}, ActionBlocker.SOURCE_GUARD_INACTIVE),
        (
            {"state": GuardState.ERROR, "error": GuardError.DOCKER_UNAVAILABLE},
            ActionBlocker.SOURCE_GUARD_INACTIVE,
        ),
        ({"zfs_state": GuardZfsState.UNAVAILABLE}, ActionBlocker.SOURCE_GUARD_INACTIVE),
        ({"shares": ("appdata", "dokumente")}, ActionBlocker.LEVELS_MISMATCH),
        ({"shares": ("appdata", "dokumente", "medien", "isos")}, ActionBlocker.LEVELS_MISMATCH),
        ({"self_dataset": GuardSelfDataset.OUTSIDE}, ActionBlocker.SELF_DATASET_MISSING),
        ({"self_dataset": GuardSelfDataset.UNKNOWN}, ActionBlocker.SELF_DATASET_MISSING),
    ],
)
async def test_blockers_from_klein(
    tmp_path: Path, change: dict[str, Any], blocker: ActionBlocker
) -> None:
    guard = ScriptedGuard()
    for key, value in change.items():
        setattr(guard, key, value)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    assert _blocker(worker) is blocker


async def test_own_dataset_counts_only_with_appdata(tmp_path: Path) -> None:
    config = CONFIG.model_copy(
        update={"sync": CONFIG.sync.model_copy(update={"shares": ["dokumente"]})}
    )
    guard = ScriptedGuard(shares=("dokumente",), self_dataset=GuardSelfDataset.MISSING)
    worker = _worker(tmp_path, guard, config=config)
    await _online(worker)
    assert _blocker(worker) is None


async def test_findings_of_the_last_check_do_not_block(tmp_path: Path) -> None:
    """Nur ein Probelauf frischt check auf; ein alter Befund darf ihn nicht sperren (4.3)."""
    guard = ScriptedGuard(check=ok_check(("dokumente",), clones=2))
    worker = _worker(tmp_path, guard)
    await _online(worker)
    assert _blocker(worker) is None


async def test_target_states(tmp_path: Path) -> None:
    worker = _worker(tmp_path, ScriptedGuard())
    # Vor der ersten Prüfung ist Groß unbekannt.
    assert _blocker(worker) is ActionBlocker.TARGET_UNKNOWN
    await worker.monitor.check_once()
    # Groß ist aus: in der Simulation darf geweckt werden.
    assert _blocker(worker) is None
    (tmp_path / "real").mkdir()
    (tmp_path / "wake").mkdir()
    real = _worker(tmp_path / "real", ScriptedGuard(), RunMode.REAL, _config(sync_dry_run=True))
    await real.monitor.check_once()
    assert _blocker(real) is ActionBlocker.TARGET_OFFLINE
    woken = _worker(
        tmp_path / "wake", ScriptedGuard(), RunMode.REAL, _config(sync_dry_run=True, wake=True)
    )
    await woken.monitor.check_once()
    assert _blocker(woken) is None


async def test_busy_target_or_running_dry_run(tmp_path: Path) -> None:
    worker = _worker(tmp_path, ScriptedGuard(), lab_timings=LabTimings(boot_seconds=60))
    await worker.monitor.check_once()
    assert await worker.actions.request(ActionName.WAKE_TARGET) is None
    assert _blocker(worker) is ActionBlocker.BUSY
    await worker.close()


async def test_tour_locks_the_dry_run(tmp_path: Path) -> None:
    worker = _worker(tmp_path, ScriptedGuard())
    set_flag(tour_flag(tmp_path), True)
    assert _blocker(worker) is ActionBlocker.TOUR
    assert _blocker(worker, ActionName.SYNC_START) is ActionBlocker.TOUR


# --- Ablauf mit dem simulierten Wächter ------------------------------------------------------


async def test_dry_run_with_the_simulated_guard(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    await _online(worker)
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    running = worker.sync.snapshot().running
    assert running is not None
    assert len(running.job_id) == 32
    assert worker.sync.is_running() is True
    # Während des Laufs ist Groß nicht auszuschalten (sync_running, Konzept 4.2).
    blocker = _blocker(worker, ActionName.SHUTDOWN_TARGET)
    assert blocker is ActionBlocker.SYNC_RUNNING
    assert _blocker(worker) is ActionBlocker.BUSY
    await _done(worker)
    status = worker.sync.snapshot()
    assert status.last is not None
    assert (status.last.outcome, status.last.reason) == (ActionOutcome.DRY_RUN_SUCCESS, None)
    report = status.report
    assert report is not None
    assert [share.name for share in report.shares] == ["appdata", "dokumente", "medien"]
    share = report.shares[0]
    assert (share.deleted, share.modified, share.added, share.renamed) == (12, 34, 56, 2)
    assert share.mode == "incremental"
    assert share.mirror is not None
    assert (share.mirror.state, share.mirror.count) == ("mirror", 5)
    assert report.send_ready is True
    assert report.awaiting_confirm is False
    assert status.levels.target == ("appdata", "dokumente", "medien")
    # Der Job ist geschlossen; danach läuft kein Kopierjob mehr.
    assert worker.sync.is_running() is False
    log = _log(tmp_path)
    assert [(entry["action"], entry["result"]) for entry in log] == [
        ("sync_dry_run", "started"),
        ("sync_dry_run", "dry_run_success"),
    ]


async def test_dry_run_wakes_the_target(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    await worker.monitor.check_once()
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    running = worker.sync.snapshot().running
    assert running is not None
    assert running.step is SyncStep.WAKING
    status, actions = worker.actions.snapshot(worker.monitor.current())
    assert status.target.state is ResourceState.STARTING
    assert actions.target.running is not None
    assert actions.target.running.action is ActionName.SYNC_DRY_RUN
    await _done(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is ActionOutcome.DRY_RUN_SUCCESS
    entries = [(e["action"], e["result"], e.get("part_of")) for e in _log(tmp_path)]
    assert entries == [
        ("sync_dry_run", "started", None),
        ("wake_target", "started", "sync_dry_run"),
        ("wake_target", "success", "sync_dry_run"),
        ("sync_dry_run", "dry_run_success", None),
    ]
    # Groß bleibt an; die Ressource ist wieder frei.
    assert worker.actions.target_busy() is False
    assert worker.monitor.current().target.state is ResourceState.ONLINE


async def test_waiting_for_the_vm_service_is_shown(tmp_path: Path) -> None:
    worker = _worker(tmp_path, lab_timings=LabTimings(boot_seconds=0.01, vm_service_seconds=60))
    await worker.monitor.check_once()
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    for _ in range(300):
        running = worker.sync.snapshot().running
        if running is not None and running.step is SyncStep.WAITING_FOR_VM_SERVICE:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("step waiting_for_vm_service not shown")
    await worker.close()


@pytest.mark.parametrize(
    ("fault", "outcome", "reason", "reasons"),
    [
        ("sync-deletions", ActionOutcome.DRY_RUN_SUCCESS, None, (GuardConfirmReason.DELETIONS,)),
        ("sync-empty-source", ActionOutcome.DRY_RUN_SUCCESS, None, (GuardConfirmReason.SHRUNK,)),
        ("sync-target-refused", ActionOutcome.TARGET_REFUSED, GuardBlocker.SPACE_LOW, ()),
        ("target-exists", ActionOutcome.TARGET_REFUSED, GuardBlocker.TARGET_EXISTS, ()),
        ("target-clone", ActionOutcome.TARGET_REFUSED, GuardBlocker.CLONE_ACTIVE, ()),
    ],
)
async def test_simulated_faults(
    tmp_path: Path,
    fault: str,
    outcome: ActionOutcome,
    reason: GuardBlocker | None,
    reasons: tuple[GuardConfirmReason, ...],
) -> None:
    set_flag(fault_flag(tmp_path, fault), True)
    worker = _worker(tmp_path)
    await _online(worker)
    await _run(worker)
    status = worker.sync.snapshot()
    assert status.last is not None
    assert (status.last.outcome, status.last.reason) == (outcome, reason)
    assert status.report is not None
    assert status.report.confirm_reasons == reasons
    assert status.report.awaiting_confirm is bool(reasons)


async def test_simulated_guard_offline_ends_the_dry_run(tmp_path: Path) -> None:
    set_flag(fault_flag(tmp_path, "source-guard-lost"), True)
    worker = _worker(tmp_path)
    await _online(worker)
    # Ein Wächter mit Fehler sperrt schon vorher.
    assert _blocker(worker) is ActionBlocker.SOURCE_GUARD_INACTIVE


# --- Ergebnisse mit dem Wächter nach Drehbuch -------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "outcome", "reason"),
    [
        (GuardOrderResult.TOO_SOON, ActionOutcome.TOO_SOON, None),
        (GuardOrderResult.SNAPSHOT_LOW_SPACE, ActionOutcome.SNAPSHOT_LOW_SPACE, None),
        (GuardOrderResult.SNAPSHOT_LIMIT, ActionOutcome.SNAPSHOT_LIMIT, None),
        (GuardOrderResult.JOB_OPEN, ActionOutcome.SOURCE_GUARD_REFUSED, GuardOrderResult.JOB_OPEN),
        (
            GuardOrderResult.DATASET_MISSING,
            ActionOutcome.SOURCE_GUARD_REFUSED,
            GuardOrderResult.DATASET_MISSING,
        ),
        (
            GuardOrderResult.SNAPSHOT_FAILED,
            ActionOutcome.SOURCE_GUARD_REFUSED,
            GuardOrderResult.SNAPSHOT_FAILED,
        ),
    ],
)
async def test_snapshot_refused(
    tmp_path: Path,
    answer: GuardOrderResult,
    outcome: ActionOutcome,
    reason: GuardOrderResult | None,
) -> None:
    guard = ScriptedGuard(answers={GuardOrder.SNAPSHOT: [answer]})
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _run(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert (last.outcome, last.reason) == (outcome, reason)
    # Ohne neuen Job gibt es nichts zu schließen; einen fremden Job fasst der Probelauf nicht an.
    assert guard.orders == [GuardOrder.SNAPSHOT]
    entries = _log(tmp_path)
    if reason is not None:
        assert entries[-1]["reason"] == reason.value


async def test_prepare_refused_closes_the_job(tmp_path: Path) -> None:
    guard = ScriptedGuard(answers={GuardOrder.PREPARE: [GuardOrderResult.RUN_OPEN]})
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _run(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert (last.outcome, last.reason) == (
        ActionOutcome.SOURCE_GUARD_REFUSED,
        GuardOrderResult.RUN_OPEN,
    )
    assert guard.orders == [GuardOrder.SNAPSHOT, GuardOrder.PREPARE, GuardOrder.CLOSE]


@pytest.mark.parametrize(
    ("state", "result", "outcome"),
    [
        (GuardJobState.FAILED, GuardJobResult.PREPARE_FAILED, ActionOutcome.PREPARE_FAILED),
        (GuardJobState.CLOSED, GuardJobResult.JOB_TIMEOUT, ActionOutcome.JOB_TIMEOUT),
        (GuardJobState.FAILED, GuardJobResult.INTERRUPTED, ActionOutcome.SOURCE_GUARD_LOST),
        (GuardJobState.CLOSED, GuardJobResult.CLOSED, ActionOutcome.SOURCE_GUARD_LOST),
    ],
)
async def test_job_ends_during_prepare(
    tmp_path: Path, state: GuardJobState, result: GuardJobResult, outcome: ActionOutcome
) -> None:
    guard = ScriptedGuard(prepared=Prepared(state=state, result=result))
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _run(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is outcome
    assert worker.sync.snapshot().report is None


async def test_own_deadline_for_prepare(tmp_path: Path) -> None:
    guard = ScriptedGuard(prepared=None)
    worker = _worker(
        tmp_path, guard, sync_timings=SyncTimings(job_poll_seconds=0.01, prepare_wait_seconds=0.05)
    )
    await _online(worker)
    await _run(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is ActionOutcome.JOB_TIMEOUT
    assert guard.orders[-1] is GuardOrder.CLOSE


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ("instance", ActionOutcome.SOURCE_GUARD_LOST),
        ("job_gone", ActionOutcome.SOURCE_GUARD_LOST),
        ("inactive", ActionOutcome.SOURCE_GUARD_INACTIVE),
        ("state_lost", ActionOutcome.SOURCE_GUARD_LOST),
    ],
)
async def test_guard_changes_during_prepare(
    tmp_path: Path, change: str, outcome: ActionOutcome
) -> None:
    def disturb(guard: ScriptedGuard) -> None:
        if guard.job is None or guard.job.state is not GuardJobState.PREPARING:
            return
        if change == "instance":
            guard.instance = "fedcba9876543210"
        elif change == "job_gone":
            guard.job = None
        elif change == "inactive":
            guard.state = GuardState.INACTIVE
        else:
            guard.state, guard.error = GuardState.ERROR, GuardError.STATE_LOST

    guard = ScriptedGuard(prepared=Prepared(delay=50), on_observe=disturb)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _run(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is outcome


async def test_guard_unreachable_for_the_first_order(tmp_path: Path) -> None:
    guard = ScriptedGuard(failing={GuardOrder.SNAPSHOT: ActionOutcome.SOURCE_GUARD_LOST})
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _run(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is ActionOutcome.SOURCE_GUARD_LOST


@pytest.mark.parametrize(
    ("prepared", "outcome", "reason"),
    [
        (Prepared(), ActionOutcome.DRY_RUN_SUCCESS, None),
        (
            Prepared(confirm_reasons=(GuardConfirmReason.NO_DIFF,)),
            ActionOutcome.DRY_RUN_SUCCESS,
            None,
        ),
        (
            Prepared(
                send_ready=False,
                blockers=(GuardBlocker.TARGET_UNREACHABLE,),
                check=failed_check(GuardCheckResult.KEY_MISSING),
            ),
            ActionOutcome.TARGET_CHECK_FAILED,
            GuardCheckResult.KEY_MISSING,
        ),
        (
            Prepared(send_ready=False, blockers=(GuardBlocker.BASE_MISSING,)),
            ActionOutcome.BASE_MISSING,
            None,
        ),
        (
            Prepared(send_ready=False, blockers=(GuardBlocker.SELF_DATASET_MISSING,)),
            ActionOutcome.SELF_DATASET_MISSING,
            None,
        ),
        (
            Prepared(
                send_ready=False,
                blockers=(GuardBlocker.DELEGATION_MISMATCH, GuardBlocker.SPACE_LOW),
            ),
            ActionOutcome.TARGET_REFUSED,
            GuardBlocker.DELEGATION_MISMATCH,
        ),
        (Prepared(send_ready=False), ActionOutcome.TARGET_REFUSED, None),
    ],
)
async def test_result_of_prepare(
    tmp_path: Path,
    prepared: Prepared,
    outcome: ActionOutcome,
    reason: GuardBlocker | GuardCheckResult | None,
) -> None:
    guard = ScriptedGuard(prepared=prepared)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    await _run(worker)
    status = worker.sync.snapshot()
    assert status.last is not None
    assert (status.last.outcome, status.last.reason) == (outcome, reason)
    assert status.report is not None
    assert status.report.blockers == prepared.blockers
    # Nach der Auswertung schließt der Probelauf seinen Job.
    assert guard.orders == [GuardOrder.SNAPSHOT, GuardOrder.PREPARE, GuardOrder.CLOSE]
    assert guard.job is not None
    assert guard.job.state is GuardJobState.CLOSED


def test_evaluate_without_check_is_lost() -> None:
    job = GuardJob.model_validate(
        {
            "id": 1,
            "state": "prepared",
            "result": None,
            "reason": None,
            "snapshot": "twinraid-20261021T120000Z",
            "created_at": "2026-10-21T12:00:00Z",
            "deadline": None,
            "confirmed": False,
            "confirm_reasons": [],
            "send_ready": True,
            "blockers": [],
            "datasets": [],
        }
    )
    assert evaluate_prepare(job, None) == (ActionOutcome.SOURCE_GUARD_LOST, None)


# --- Abbrechen und Ende des Workers ----------------------------------------------------------


async def test_cancel_during_prepare(tmp_path: Path) -> None:
    guard = ScriptedGuard(prepared=None)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    for _ in range(300):
        if GuardOrder.PREPARE in guard.orders:
            break
        await asyncio.sleep(0.01)
    running = worker.sync.snapshot().running
    assert running is not None
    assert worker.sync.cancel("0" * 32) is False
    assert worker.sync.cancel(running.job_id) is True
    await _done(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is ActionOutcome.CANCELLED
    assert guard.orders[-1] is GuardOrder.CLOSE
    assert _log(tmp_path)[-1]["result"] == "cancelled"


async def test_cancel_while_the_snapshot_order_is_open(tmp_path: Path) -> None:
    """Die Antwort auf snapshot kam nicht mehr an; der neue Job wird trotzdem geschlossen."""
    guard = ScriptedGuard(hold=GuardOrder.SNAPSHOT)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    for _ in range(300):
        if guard.job is not None:
            break
        await asyncio.sleep(0.01)
    running = worker.sync.snapshot().running
    assert running is not None
    assert worker.sync.cancel(running.job_id) is True
    await _done(worker)
    assert guard.orders == [GuardOrder.SNAPSHOT, GuardOrder.CLOSE]
    assert guard.job is not None
    assert guard.job.state is GuardJobState.CLOSED


async def test_cancel_while_waking(tmp_path: Path) -> None:
    worker = _worker(tmp_path, lab_timings=LabTimings(boot_seconds=60))
    await worker.monitor.check_once()
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    await asyncio.sleep(0.05)
    running = worker.sync.snapshot().running
    assert running is not None
    assert worker.actions.running_step("x") is None
    assert worker.sync.cancel(running.job_id) is True
    await _done(worker)
    entries = [(e["action"], e["result"]) for e in _log(tmp_path)]
    assert entries == [
        ("sync_dry_run", "started"),
        ("wake_target", "started"),
        ("wake_target", "cancelled"),
        ("sync_dry_run", "cancelled"),
    ]
    assert worker.actions.target_busy() is False


async def test_target_is_reserved_when_the_dry_run_is_accepted(tmp_path: Path) -> None:
    """Zwischen Annahme und Wecken kann keine andere Aktion Groß belegen (Konzept 4.2)."""
    worker = _worker(tmp_path, lab_timings=LabTimings(boot_seconds=60))
    await worker.monitor.check_once()
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    # Noch im selben Schritt der Ereignisschleife, bevor der Ablauf begonnen hat.
    assert worker.actions.target_busy() is True
    assert await worker.actions.request(ActionName.WAKE_TARGET) is ActionBlocker.BUSY
    await worker.close()


async def test_cancel_before_the_first_step(tmp_path: Path) -> None:
    worker = _worker(tmp_path, lab_timings=LabTimings(boot_seconds=60))
    await worker.monitor.check_once()
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    running = worker.sync.snapshot().running
    assert running is not None
    assert worker.sync.cancel(running.job_id) is True
    await _done(worker)
    last = worker.sync.snapshot().last
    assert last is not None
    assert last.outcome is ActionOutcome.CANCELLED
    assert worker.actions.target_busy() is False
    assert _log(tmp_path)[-1]["result"] == "cancelled"


async def test_worker_end_interrupts_the_dry_run(tmp_path: Path) -> None:
    guard = ScriptedGuard(prepared=None)
    worker = _worker(tmp_path, guard)
    await _online(worker)
    assert await worker.actions.request(ActionName.SYNC_DRY_RUN) is None
    for _ in range(300):
        if GuardOrder.PREPARE in guard.orders:
            break
        await asyncio.sleep(0.01)
    await worker.close()
    assert _log(tmp_path)[-1]["result"] == "interrupted"
    # Den offenen Job schließt der Worker beim nächsten Start (Konzept 3.6).
    assert guard.orders == [GuardOrder.SNAPSHOT, GuardOrder.PREPARE]


# --- Nach dem Start des Workers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "closed"),
    [
        (GuardJobState.SNAPSHOT, True),
        (GuardJobState.PREPARED, True),
        (GuardJobState.AWAITING_CONFIRM, True),
        (GuardJobState.SENDING, False),
        (GuardJobState.DONE, False),
    ],
)
async def test_stale_job_is_closed_after_the_start(
    tmp_path: Path, state: GuardJobState, closed: bool
) -> None:
    guard = ScriptedGuard()
    await guard.order(GuardOrder.SNAPSHOT)
    assert guard.job is not None
    guard.job = guard.job.model_copy(update={"state": state})
    guard.orders.clear()
    worker = _worker(tmp_path, guard)
    assert worker.sync.is_running() is (state is not GuardJobState.DONE)
    await worker.sync.run()
    assert guard.orders == ([GuardOrder.CLOSE] if closed else [])


async def test_startup_waits_for_an_active_guard(tmp_path: Path) -> None:
    guard = ScriptedGuard(state=GuardState.INACTIVE)
    worker = _worker(tmp_path, guard)
    task = asyncio.create_task(worker.sync.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    guard.state = GuardState.ACTIVE
    await asyncio.wait_for(task, 2)


# --- Ebenen -----------------------------------------------------------------------------------


async def test_levels(tmp_path: Path) -> None:
    guard = ScriptedGuard(shares=("appdata", "dokumente"))
    worker = _worker(tmp_path, guard)
    levels = worker.sync.snapshot().levels
    assert levels.configured == ("appdata", "dokumente", "medien")
    assert levels.source == ("appdata", "dokumente")
    assert (levels.target, levels.target_checked_at) == (None, None)
    guard.check = ok_check(("appdata",))
    levels = worker.sync.snapshot().levels
    assert levels.target == ("appdata",)
    assert levels.target_checked_at == guard.check.at
    # Ein gescheitertes check sagt nichts über mirror.conf.
    guard.check = failed_check(GuardCheckResult.UNREACHABLE)
    assert worker.sync.snapshot().levels.target is None
    guard.state = GuardState.INACTIVE
    assert worker.sync.snapshot().levels.source is None


async def test_running_step_only_for_the_own_run(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    assert worker.actions.running_step("abc") is None
    assert worker.actions.wake_enabled() is True
    _ = ActionStep.WAKING
