"""Wählt je nach Betriebsmodus echte Integrationen oder Simulationen."""

from pathlib import Path

from twinraid.config import TwinRaidConfig
from twinraid.integrations.jobs import JobState, NoJobs
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.status import MockStatusProvider
from twinraid.integrations.status import RealStatusProvider, StatusProvider
from twinraid.integrations.unraid_api import ApiEndpoint, Credentials, UnraidApi
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.mode import RunMode


def target_api(config: TwinRaidConfig, paths: DataPaths) -> UnraidApi:
    credentials = Credentials(paths.target_api_key, paths.target_api_cert)
    return UnraidApi(ApiEndpoint.of(config.servers.target), credentials)


def build_status_provider(
    mode: RunMode, config: TwinRaidConfig, paths: DataPaths
) -> StatusProvider:
    if mode is RunMode.MOCK:
        return MockStatusProvider()
    return RealStatusProvider(config, target_api(config, paths))


def build_job_state(mode: RunMode, mock_flag: Path) -> JobState:
    if mode is RunMode.MOCK:
        return MockJobSwitch(mock_flag)
    # Der echte Sync folgt in Phase 5.
    return NoJobs()
