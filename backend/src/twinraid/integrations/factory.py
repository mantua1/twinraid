"""Wählt je nach Betriebsmodus echte Integrationen oder Simulationen."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from twinraid.config import TwinRaidConfig
from twinraid.integrations.control import ActionControl, RealControl
from twinraid.integrations.jobs import JobState, NoJobs
from twinraid.integrations.mocks.jobs import MockJobSwitch
from twinraid.integrations.mocks.lab import MockLab
from twinraid.integrations.ssh import GuardKey, SshPower, SshTarget
from twinraid.integrations.status import RealStatusProvider, StatusProvider
from twinraid.integrations.vm_watch import VmWatch
from twinraid.integrations.wol import WolTarget
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.mode import RunMode

# Any: Rückgabetypen von Coroutinen, die der Worker nur als Aufgabe startet.
BackgroundTask = Callable[[], Coroutine[Any, Any, None]]


def target_ssh(config: TwinRaidConfig, paths: DataPaths) -> SshPower:
    """Aktionen mit dem Key „power“."""
    return SshPower(SshTarget.of(config, paths, GuardKey.POWER))


def target_watch(config: TwinRaidConfig, paths: DataPaths) -> VmWatch:
    """Sitzung vm-watch mit dem Key „status“; beim Wecken darf sshd bis boot_timeout fehlen."""
    target = config.servers.target
    return VmWatch(SshTarget.of(config, paths, GuardKey.STATUS), target.boot_timeout_minutes * 60.0)


def _never() -> bool:
    return False


def _nothing() -> None:
    """Simulationsmodus: Es gibt keine Statusverbindung, die warten könnte."""


@dataclass(frozen=True)
class Integrations:
    """Status für den Monitor und Aktionen für die Abläufe des Workers."""

    status: StatusProvider
    control: ActionControl
    # Simulationsmodus: Der feste Rundlauf ist eingeschaltet (twinraid-admin mock-tour).
    tour_active: Callable[[], bool]
    # Aufgaben, die der Worker dauerhaft laufen lässt (Modus real: die Sitzung vm-watch).
    background: tuple[BackgroundTask, ...] = ()
    # Gesetzt, wenn sich der Status ohne neue Prüfung geändert hat; der Monitor prüft dann sofort.
    status_changed: asyncio.Event | None = None
    # Beendet jede Wartezeit der Statusverbindung (twinraid-admin trust-host-key, ab v0.4.4).
    retry_status_now: Callable[[], None] = _nothing


def build_integrations(
    mode: RunMode,
    config: TwinRaidConfig,
    paths: DataPaths,
    mock_flag_dir: Path,
    wol: WolTarget | None,
) -> Integrations:
    if mode is RunMode.MOCK:
        lab = MockLab(mock_flag_dir)
        return Integrations(lab, lab, lab.tour_active)
    watch = target_watch(config, paths)
    control = RealControl(config, watch, target_ssh(config, paths), wol)
    return Integrations(
        RealStatusProvider(config, watch),
        control,
        _never,
        background=(watch.run,),
        status_changed=watch.changed,
        retry_status_now=watch.retry_now,
    )


def build_job_state(mode: RunMode, mock_flag: Path) -> JobState:
    if mode is RunMode.MOCK:
        return MockJobSwitch(mock_flag)
    # Der echte Sync folgt in Phase 5.
    return NoJobs()
