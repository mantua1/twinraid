"""Startpunkt des Worker-Prozesses (twinraid-worker)."""

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from twinraid.config import ConfigError, TwinRaidConfig, load_config
from twinraid.integrations.factory import BackgroundTask, build_integrations, build_job_state
from twinraid.integrations.wol import BroadcastError, WolTarget, resolve_wol_target
from twinraid.shared import runtime
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.logs import configure_logging, warn_if_mock
from twinraid.shared.mode import RunMode, RunModeError, resolve_run_mode
from twinraid.shared.passwords import PasswordStore
from twinraid.worker.action_log import ActionLog
from twinraid.worker.actions import ActionCoordinator
from twinraid.worker.handler import CommandHandler
from twinraid.worker.monitor import StatusMonitor
from twinraid.worker.password_check import PasswordCheck
from twinraid.worker.server import SocketPathError, WorkerServer

logger = logging.getLogger("twinraid.worker")

EXIT_STARTUP_ERROR = 2
HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_FILE = "worker.heartbeat"


def main() -> int:
    # Der Worker legt nie Dateien an, die Gruppe oder andere lesen dürfen.
    os.umask(0o077)
    configure_logging("worker")
    env = os.environ
    try:
        mode = resolve_run_mode(env)
        allowed_uid = runtime.web_uid(env)
    except (RunModeError, runtime.RuntimeSettingError) as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR
    warn_if_mock(logger, mode)

    try:
        config = load_config(runtime.config_path(env))
    except ConfigError as exc:
        logger.error("%s", exc.render())
        return EXIT_STARTUP_ERROR

    try:
        wol = wake_on_lan(config, mode)
    except BroadcastError as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR

    paths = DataPaths(runtime.data_dir(env))
    password_check = PasswordCheck(PasswordStore(paths.password_file))
    integrations = build_integrations(mode, config, paths, runtime.mock_flag_dir(env), wol)
    monitor = StatusMonitor(integrations.status, changes=integrations.status_changed)
    jobs = build_job_state(mode, runtime.mock_job_flag(env))
    actions = ActionCoordinator(
        integrations.control,
        monitor,
        jobs,
        config,
        mode,
        ActionLog(paths.action_log),
        tour_active=integrations.tour_active,
    )
    handler = CommandHandler(monitor, password_check, jobs, mode, actions)
    server = WorkerServer(runtime.socket_path(env), allowed_uid, handler)
    log_enabled_actions(actions, mode)
    try:
        asyncio.run(_serve(server, password_check, monitor, actions, integrations.background))
    except SocketPathError as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR
    return 0


def wake_on_lan(config: TwinRaidConfig, mode: RunMode) -> WolTarget | None:
    """Broadcast-Adresse für Wake-on-LAN (Konzept 4.2).

    Lässt sie sich nicht ableiten, startet der Worker nur dann nicht, wenn Wecken im
    Modus real freigeschaltet ist; sonst gibt es eine Warnung.
    """
    if mode is RunMode.MOCK:
        return None
    try:
        wol = resolve_wol_target(config.servers.target)
    except BroadcastError as exc:
        if config.actions.wake:
            raise
        logger.warning("Wake-on-LAN is not available: %s", exc)
        return None
    logger.info("Wake-on-LAN broadcast address: %s (%s)", wol.broadcast, wol.origin)
    return wol


def log_enabled_actions(actions: ActionCoordinator, mode: RunMode) -> None:
    if mode is not RunMode.REAL:
        return
    enabled = actions.enabled_groups()
    if not enabled:
        logger.info("Read-only real mode: status checks only, all actions are locked.")
        return
    names = ", ".join(group.value for group in enabled)
    logger.warning("Real mode: these actions are enabled in twinraid.yaml: %s", names)


def _touch(path: Path) -> None:
    path.touch(mode=0o600)
    os.utime(path)


async def heartbeat(path: Path, interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """Aktualisiert regelmäßig eine Datei; der Docker-Healthcheck prüft ihr Alter."""
    while True:
        await asyncio.to_thread(_touch, path)
        await asyncio.sleep(interval)


async def _serve(
    server: WorkerServer,
    password_check: PasswordCheck,
    monitor: StatusMonitor,
    actions: ActionCoordinator,
    background: tuple[BackgroundTask, ...] = (),
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)

    def reset_throttle() -> None:
        password_check.reset()
        logger.warning("Login throttling in the worker was reset (twinraid-admin unlock).")

    # SIGUSR1 kommt von "twinraid-admin unlock" über s6-svc -1.
    loop.add_signal_handler(signal.SIGUSR1, reset_throttle)
    await server.start()
    tasks = [
        asyncio.create_task(heartbeat(server.path.parent / HEARTBEAT_FILE)),
        asyncio.create_task(monitor.run()),
        *(asyncio.create_task(task()) for task in background),
    ]
    logger.info("worker listening on %s", server.path)
    try:
        await stop.wait()
    finally:
        await actions.close()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await server.close()
        logger.info("worker stopped")
