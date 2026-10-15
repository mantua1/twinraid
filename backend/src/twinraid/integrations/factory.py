"""Wählt je nach Betriebsmodus echte Integrationen oder Simulationen."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from twinraid.config import TwinRaidConfig
from twinraid.integrations.control import ActionControl, RealControl
from twinraid.integrations.jobs import JobState, NoJobs
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab
from twinraid.integrations.ssh import SshPower, SshTarget
from twinraid.integrations.status import RealStatusProvider, StatusProvider
from twinraid.integrations.unraid_api import ApiEndpoint, Credentials, UnraidApi
from twinraid.integrations.wol import WolTarget
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.mode import RunMode


def target_api(config: TwinRaidConfig, paths: DataPaths) -> UnraidApi:
    credentials = Credentials(paths.target_api_key, paths.target_api_cert)
    return UnraidApi(ApiEndpoint.of(config.servers.target), credentials)


def target_ssh(config: TwinRaidConfig, paths: DataPaths) -> SshPower:
    return SshPower(SshTarget.of(config, paths))


def _never() -> bool:
    return False


@dataclass(frozen=True)
class Integrations:
    """Status für den Monitor und Aktionen für die Abläufe des Workers."""

    status: StatusProvider
    control: ActionControl
    # Simulationsmodus: Der feste Rundlauf ist eingeschaltet (twinraid-admin mock-tour).
    tour_active: Callable[[], bool]


def build_integrations(
    mode: RunMode,
    config: TwinRaidConfig,
    paths: DataPaths,
    mock_flag_dir: Path,
    wol: WolTarget | None,
) -> Integrations:
    if mode is RunMode.MOCK:
        lab = MockLab(config.gaming_vm.unraid_vm_name, mock_flag_dir)
        return Integrations(lab, lab, lab.tour_active)
    api = target_api(config, paths)
    control = RealControl(config, api, target_ssh(config, paths), wol)
    return Integrations(RealStatusProvider(config, api), control, _never)


def build_job_state(mode: RunMode, mock_flag: Path) -> JobState:
    if mode is RunMode.MOCK:
        return MockJobSwitch(mock_flag)
    # Der echte Sync folgt in Phase 5.
    return NoJobs()
