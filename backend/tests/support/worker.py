"""Worker-Handler für Tests: ungeprüfter Status, Job-Schalter, wählbarer Modus."""

from pathlib import Path

from tests.conftest import EXAMPLE_CONFIG
from tests.support.passwords import TEST_PASSWORD, password_check
from twinraid.config import TwinRaidConfig, load_config
from twinraid.integrations.control import ActionControl
from twinraid.integrations.jobs import JobState
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab
from twinraid.shared.mode import RunMode
from twinraid.worker.action_log import ActionLog
from twinraid.worker.actions import ActionCoordinator, StatusSource
from twinraid.worker.handler import CommandHandler
from twinraid.worker.monitor import StatusMonitor


def make_handler(
    directory: Path,
    mode: RunMode = RunMode.MOCK,
    status: StatusSource | None = None,
    jobs: JobState | None = None,
    password: str | None = TEST_PASSWORD,
    config: TwinRaidConfig | None = None,
    control: ActionControl | None = None,
) -> CommandHandler:
    config = config or load_config(EXAMPLE_CONFIG)
    lab = MockLab(config.gaming_vm.unraid_vm_name, directory)
    monitor = status or StatusMonitor(lab)
    job_state = jobs or MockJobSwitch(directory / "mock-job-running")
    actions = ActionCoordinator(
        control or lab,
        monitor,
        job_state,
        config,
        mode,
        ActionLog(directory / "actions.jsonl"),
        tour_active=lab.tour_active,
    )
    return CommandHandler(monitor, password_check(directory, password), job_state, mode, actions)
