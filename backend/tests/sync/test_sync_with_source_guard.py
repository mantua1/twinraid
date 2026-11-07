"""Der Worker treibt den echten source-guard.sh (Konzept 4.3 und 5.6, ab v0.5.2).

Der Wächter läuft wie in tests/test_source_guard_jobs.py als Kopie mit nachgebautem docker,
zfs und ssh; TwinRaid spricht ihn über RealGuardChannel an, also über dieselbe
Auftragsdatei und status.json wie auf Klein. Geprüft werden die Folge der Aufträge, die
Zeile mit Container, die Wartezeit von stop, ping im Takt, Freigabe, Fortschritt und
Abbrechen.
"""

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tests.support.guard_status import mountinfo
from tests.support.source_guard_host import Host, container_id, kill_leftovers, make_host
from tests.support.worker import TestWorker, make_worker
from tests.test_source_guard_jobs import CONF, check_answer, setup, started
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.guard_orders import OrderTimings, RealGuardChannel
from twinraid.integrations.mocks.lab import LabTimings
from twinraid.integrations.source_guard import GuardPaths, GuardReport, evaluate, read_observation
from twinraid.shared.actions import ActionBlocker, ActionName, ActionOutcome
from twinraid.shared.mode import RunMode
from twinraid.shared.source_guard import GuardOrder
from twinraid.shared.status import ResourceState
from twinraid.shared.sync import SyncStep
from twinraid.sync.coordinator import SyncTimings

pytestmark = pytest.mark.anyio

CONTAINER = "twinraid-testcontainer"
FAST_LAB = LabTimings(
    boot_seconds=0.05,
    vm_service_seconds=0.05,
    vm_start_seconds=0.01,
    vm_stop_seconds=0.01,
    shutdown_seconds=0.01,
)
# Der Wächter nimmt höchstens einen Auftrag pro Sekunde an; ping im Takt von 2 s.
TIMINGS = SyncTimings(job_poll_seconds=0.1, startup_poll_seconds=0.1, ping_seconds=2.0)


@dataclass(frozen=True)
class HostPaths(GuardPaths):
    """Die Pfade des Testaufbaus: Auftragsdatei in in/, Status in state/."""

    host_base: Path = Path()

    @property
    def request(self) -> Path:
        return self.host_base / "in" / "request"

    @property
    def state(self) -> Path:
        return self.host_base / "state"


class HostGuard:
    """Status für Verfügbarkeit und Kachel, frisch gelesen wie RealSourceGuard."""

    def __init__(self, paths: GuardPaths) -> None:
        self._paths = paths

    def report(self) -> GuardReport:
        return evaluate(read_observation(self._paths), datetime.now(UTC))


def _paths(host: Host) -> HostPaths:
    info = host.tmp / "mountinfo"
    text = mountinfo(host.base)
    # Die Einbindungen liegen im Testaufbau unter in/request und state.
    text = text.replace(f"{host.base}/request", f"{host.base}/in/request")
    info.write_text(text)
    return HostPaths(mountinfo=info, host_base=host.base)


def _config(**sync: Any) -> TwinRaidConfig:  # noqa: ANN401 - Werte der Konfiguration
    config = load_config(EXAMPLE_CONFIG)
    values = {"shares": ["twinraidtest"], **sync}
    return config.model_copy(update={"sync": config.sync.model_copy(update=values)})


@pytest.fixture
def host(tmp_path: Path) -> Iterator[Host]:
    directory = tmp_path / "klein"
    directory.mkdir()
    instance = make_host(directory)
    setup(instance)
    yield instance
    kill_leftovers(tmp_path)


async def _worker(
    host: Host, config: TwinRaidConfig | None = None, directory: str = "worker"
) -> TestWorker:
    paths = _paths(host)
    channel = RealGuardChannel(paths, OrderTimings(poll_seconds=0.05))
    worker = make_worker(
        host.tmp / directory,
        RunMode.MOCK,
        config=config or _config(),
        source_guard=HostGuard(paths),
        channel=channel,
        lab_timings=FAST_LAB,
        sync_timings=TIMINGS,
    )
    await worker.lab.wake()
    for _ in range(300):
        status = await worker.monitor.check_once()
        if status.target.state is ResourceState.ONLINE and status.vm.reason is None:
            return worker
        await asyncio.sleep(0.01)
    raise AssertionError("simulated target did not come up")


async def _until(predicate: Callable[[], bool], seconds: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


def _step(worker: TestWorker) -> SyncStep | None:
    running = worker.sync.snapshot().running
    return running.step if running is not None else None


def _outcome(worker: TestWorker) -> tuple[ActionOutcome, object]:
    last = worker.sync.snapshot().last
    assert last is not None
    return last.outcome, last.reason


def _orders(host: Host) -> list[str]:
    """Angenommene Aufträge aus dem Syslog des Wächters, in ihrer Reihenfolge."""
    # Zeilen wie "order 5: stop <Name>: stopped" oder "order 3: begin: opened".
    return [line.split(": ")[1].split()[0] for line in host.syslog() if line.startswith("order ")]


async def test_sync_against_the_real_guard(host: Host, tmp_path: Path) -> None:
    (tmp_path / "klein" / "worker").mkdir()
    started(host)
    worker = await _worker(host)
    assert await worker.actions.request(ActionName.SYNC_START) is None
    await _until(lambda: worker.sync.snapshot().running is None)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    # Nur der laufende freigegebene Container wird angehalten und wieder gestartet.
    changes = host.docker_changes()
    assert [change[0] for change in changes] == ["stop", "start"]
    assert changes[0][-1] == container_id(CONTAINER)
    assert host.running(CONTAINER)
    status = host.status()
    assert status["run"] is None
    assert status["last_run"]["reason"] == "finish"
    assert status["job"]["state"] == "done"
    assert status["job"]["datasets"][0]["result"] == "ok"
    assert [received["name"] for received in host.received()] == ["twinraidtest"]
    # Container-Name nur in der Zeile an den Wächter, nie in Aktionslog oder Historie.
    worker_dir = tmp_path / "klein" / "worker"
    assert CONTAINER not in (worker_dir / "actions.jsonl").read_text()
    assert CONTAINER not in (worker_dir / "sync-history.jsonl").read_text()
    history = worker.sync.snapshot().history[0]
    assert (history.containers_stopped, history.result) == (1, ActionOutcome.SUCCESS)
    running = worker.sync.snapshot().report
    assert running is not None
    assert running.shares[0].sent == running.shares[0].size


async def test_orders_and_lines(host: Host, tmp_path: Path) -> None:
    (tmp_path / "klein" / "worker").mkdir()
    started(host)
    worker = await _worker(host)
    assert await worker.actions.request(ActionName.SYNC_START) is None
    await _until(lambda: worker.sync.snapshot().running is None)
    status = host.status()
    assert status["rejected"]["count"] == 0
    orders = [order for order in _orders(host) if order != "ping"]
    assert orders == ["begin", "stop", "snapshot", "finish", "prepare", "send"]
    # stop trägt den Namen; das Syslog von Klein nennt ihn (Konzept 3.6).
    assert any(line.startswith("order ") and CONTAINER in line for line in host.syslog())


async def test_confirmation_against_the_real_guard(host: Host, tmp_path: Path) -> None:
    (tmp_path / "klein" / "worker").mkdir()
    host.configure(CONF + "max_deletions 1\n")
    started(host)
    worker = await _worker(host)
    assert await worker.actions.request(ActionName.SYNC_START) is None
    await _until(lambda: _step(worker) is SyncStep.AWAITING_CONFIRM)
    running = worker.sync.snapshot().running
    assert running is not None
    assert running.confirmation is not None
    assert (running.confirmation.deletions, running.confirmation.max_deletions) == (2, 1)
    assert running.confirmation.deadline is not None
    assert host.status()["job"]["state"] == "awaiting_confirm"
    assert worker.sync.confirm(running.job_id) is True
    await _until(lambda: worker.sync.snapshot().running is None)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)
    assert host.status()["job"]["confirmed"] is True


async def test_cancel_while_sending(host: Host, tmp_path: Path) -> None:
    (tmp_path / "klein" / "worker").mkdir()
    host.update_zfs(lambda z: z.update(send_seconds=30))
    started(host)
    worker = await _worker(host)
    assert await worker.actions.request(ActionName.SYNC_START) is None

    def progressing() -> bool:
        running = worker.sync.snapshot().running
        return running is not None and running.progress is not None and running.progress.sent > 0

    await _until(progressing)
    running = worker.sync.snapshot().running
    assert running is not None
    assert running.step is SyncStep.SENDING
    assert worker.sync.cancel(running.job_id) is True
    await _until(lambda: worker.sync.snapshot().running is None)
    assert _outcome(worker) == (ActionOutcome.CANCELLED, None)
    job = host.status()["job"]
    assert (job["state"], job["result"]) == ("closed", "closed")


async def test_stop_waits_for_a_slow_container(host: Host, tmp_path: Path) -> None:
    """Ein langsames docker stop: Der Worker wartet bis stop_timeout + 15 s (Konzept 3.6)."""
    (tmp_path / "klein" / "worker").mkdir()
    host.change_container(CONTAINER, stop_seconds=3)
    started(host)
    worker = await _worker(host)
    assert await worker.actions.request(ActionName.SYNC_START) is None
    await _until(lambda: worker.sync.snapshot().running is None)
    assert _outcome(worker) == (ActionOutcome.SUCCESS, None)


async def test_pause_blocks_the_next_sync(host: Host, tmp_path: Path) -> None:
    (tmp_path / "klein" / "worker").mkdir()
    started(host)
    worker = await _worker(host)
    assert await worker.actions.request(ActionName.SYNC_START) is None
    await _until(lambda: worker.sync.snapshot().running is None)
    await _until(lambda: host.status()["run_allowed_at"] is not None, 10)
    availability = worker.actions.availability(worker.monitor.current())
    assert availability[ActionName.SYNC_START] is ActionBlocker.SOURCE_GUARD_TOO_SOON


async def test_worker_restart_adopts_the_transfer(host: Host, tmp_path: Path) -> None:
    (tmp_path / "klein" / "worker").mkdir()
    host.update_zfs(lambda z: z.update(send_seconds=6))
    started(host)
    worker = await _worker(host)
    assert await worker.actions.request(ActionName.SYNC_START) is None
    await _until(lambda: (host.status()["job"] or {}).get("state") == "sending")
    await worker.close()
    assert host.status()["job"]["state"] == "sending"
    (tmp_path / "klein" / "worker2").mkdir()
    again = make_worker(
        tmp_path / "klein" / "worker2",
        RunMode.MOCK,
        config=_config(),
        source_guard=HostGuard(_paths(host)),
        channel=RealGuardChannel(_paths(host), OrderTimings(poll_seconds=0.05)),
        lab_timings=FAST_LAB,
        sync_timings=TIMINGS,
    )
    await again.sync.run()
    running = again.sync.snapshot().running
    assert running is not None
    assert running.adopted is True
    await _until(lambda: again.sync.snapshot().running is None)
    assert _outcome(again) == (ActionOutcome.SUCCESS, None)
    assert GuardOrder.CLOSE.value not in _orders(host)


def _candidates(paths: GuardPaths) -> frozenset[str]:
    return HostGuard(paths).report().hold_candidates


def _mirror_at_newest(host: Host) -> None:
    """Groß meldet im nächsten check den zuletzt gesendeten Snapshot als neuesten."""
    zfs = host.zfs()
    name = max(
        (key for key in zfs["snapshots"] if key.startswith("mainpool/twinraidtest@")),
        key=lambda key: zfs["snapshots"][key]["createtxg"],
    )
    snapshot = zfs["snapshots"][name]
    line = (
        f"state=mirror props=ok count=4 newest_guid={snapshot['guid']} "
        f"newest_created={snapshot['creation']} newest_received={snapshot['creation'] + 5} "
        "oldest_created=1750000000"
    )
    host.answer_ssh(check=check_answer({"twinraidtest": line}), receive={"default": {"rc": 0}})


async def _run(worker: TestWorker, name: ActionName) -> ActionOutcome:
    assert await worker.actions.request(name) is None
    await _until(lambda: worker.sync.snapshot().running is None)
    last = worker.sync.snapshot().last
    assert last is not None
    return last.outcome


async def test_hold_notice_stays_until_a_successful_sync(host: Host, tmp_path: Path) -> None:
    """Konzept 3.6: Der Hinweis bleibt nach dem Ende des Jobs, nach einem Neustart von TwinRaid
    und nach einem Probelauf; erst ein erfolgreicher Sync mit Hold beseitigt ihn."""
    config = _config(stop_containers=False)
    paths = _paths(host)
    host.update_zfs(lambda z: z.update(fail=["hold"]))
    started(host)
    (tmp_path / "klein" / "worker").mkdir()
    worker = await _worker(host, config)
    # Der Hold an der neuen Basis scheitert; gesendet ist trotzdem.
    assert await _run(worker, ActionName.SYNC_START) is ActionOutcome.SUCCESS
    await _until(lambda: _candidates(paths) == frozenset({"twinraidtest"}), 10)
    # Der Job ist zu Ende und nicht mehr offen.
    assert host.status()["job"]["state"] == "done"
    await worker.close()

    # Neustart von TwinRaid: neuer Worker, der Hinweis kommt wieder aus dem Status.
    (tmp_path / "klein" / "worker2").mkdir()
    again = await _worker(host, config, "worker2")
    await again.sync.run()
    assert _candidates(paths) == frozenset({"twinraidtest"})

    # Probelauf: Sein Job ersetzt den gesendeten; der neue check kennt den gesendeten Snapshot.
    _mirror_at_newest(host)
    host.update_zfs(lambda z: z.update(fail=[]))
    await asyncio.sleep(1.1)  # zwei Snapshots in derselben Sekunde hätten denselben Namen
    assert await _run(again, ActionName.SYNC_DRY_RUN) is ActionOutcome.DRY_RUN_SUCCESS
    assert host.status()["job"]["result"] == "closed"
    assert _candidates(paths) == frozenset({"twinraidtest"})

    # Erst ein erfolgreicher Sync setzt den Hold neu.
    await asyncio.sleep(1.1)
    assert await _run(again, ActionName.SYNC_START) is ActionOutcome.SUCCESS
    await _until(lambda: _candidates(paths) == frozenset(), 10)
    await again.close()
