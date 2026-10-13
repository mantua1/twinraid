"""Worker-Handler für Tests: ungeprüfter Status, Job-Schalter, wählbarer Modus."""

from pathlib import Path

from tests.support.passwords import TEST_PASSWORD, password_check
from twinraid.integrations.jobs import JobState
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.status import MockStatusProvider
from twinraid.shared.mode import RunMode
from twinraid.worker.handler import CommandHandler, StatusSource
from twinraid.worker.monitor import StatusMonitor


def make_handler(
    directory: Path,
    mode: RunMode = RunMode.MOCK,
    status: StatusSource | None = None,
    jobs: JobState | None = None,
    password: str | None = TEST_PASSWORD,
) -> CommandHandler:
    return CommandHandler(
        status or StatusMonitor(MockStatusProvider()),
        password_check(directory, password),
        jobs or MockJobSwitch(directory / "mock-job-running"),
        mode,
    )
