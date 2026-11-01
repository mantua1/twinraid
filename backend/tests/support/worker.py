"""Worker für Tests: Handler, Aktionen und Probelauf, verbunden wie in worker/main.py."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from tests.conftest import EXAMPLE_CONFIG
from tests.support.passwords import TEST_PASSWORD, password_check
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.control import ActionControl
from twinraid.integrations.guard_orders import GuardChannel
from twinraid.integrations.jobs import JobState
from twinraid.integrations.mocks.guard_jobs import MockJobTimings
from twinraid.integrations.mocks.lab import LabTimings, MockLab
from twinraid.integrations.mocks.source_guard import MockSourceGuard
from twinraid.integrations.source_guard import SourceGuardSource
from twinraid.shared.mode import RunMode
from twinraid.sync.coordinator import SyncCoordinator, SyncTimings
from twinraid.worker.action_log import ActionLog
from twinraid.worker.actions import ActionCoordinator, ActionTimings, StatusSource
from twinraid.worker.handler import CommandHandler
from twinraid.worker.monitor import StatusMonitor

# Kurze Zeiten für den simulierten Wächter und den Probelauf.
FAST_JOBS = MockJobTimings(snapshot_seconds=0.0, prepare_seconds=0.05)
FAST_SYNC = SyncTimings(job_poll_seconds=0.01, startup_poll_seconds=0.01)


@dataclass(frozen=True)
class TestWorker:
    __test__: ClassVar[bool] = False

    handler: CommandHandler
    actions: ActionCoordinator
    sync: SyncCoordinator
    lab: MockLab
    monitor: StatusSource

    async def close(self) -> None:
        await self.sync.close()
        await self.actions.close()


def make_worker(
    directory: Path,
    mode: RunMode = RunMode.MOCK,
    status: StatusSource | None = None,
    jobs: JobState | None = None,
    password: str | None = TEST_PASSWORD,
    config: TwinRaidConfig | None = None,
    control: ActionControl | None = None,
    source_guard: SourceGuardSource | None = None,
    channel: GuardChannel | None = None,
    action_timings: ActionTimings | None = None,
    lab_timings: LabTimings | None = None,
    sync_timings: SyncTimings = FAST_SYNC,
) -> TestWorker:
    config = config or load_config(EXAMPLE_CONFIG)
    lab = MockLab(directory, lab_timings)
    monitor = status or StatusMonitor(lab)
    mock_guard = MockSourceGuard(directory, timings=FAST_JOBS, datasets=tuple(config.sync.shares))
    guard = source_guard or mock_guard
    action_log = ActionLog(directory / "actions.jsonl")
    sync = SyncCoordinator(
        channel or mock_guard, guard, monitor, config, mode, action_log, timings=sync_timings
    )
    job_state = jobs or sync
    actions = ActionCoordinator(
        control or lab,
        monitor,
        job_state,
        config,
        mode,
        action_log,
        tour_active=lab.tour_active,
        timings=action_timings,
    )
    actions.attach_sync(sync)
    sync.attach(actions)
    handler = CommandHandler(
        monitor, password_check(directory, password), job_state, mode, actions, guard, sync
    )
    return TestWorker(handler, actions, sync, lab, monitor)


def make_handler(
    directory: Path,
    mode: RunMode = RunMode.MOCK,
    status: StatusSource | None = None,
    jobs: JobState | None = None,
    password: str | None = TEST_PASSWORD,
    config: TwinRaidConfig | None = None,
    control: ActionControl | None = None,
    source_guard: SourceGuardSource | None = None,
) -> CommandHandler:
    return make_worker(
        directory, mode, status, jobs, password, config, control, source_guard
    ).handler
