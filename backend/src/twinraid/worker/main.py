"""Startpunkt des Worker-Prozesses (twinraid-worker)."""

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from twinraid.config import ConfigError, load_config
from twinraid.integrations.factory import build_status_provider
from twinraid.shared import runtime
from twinraid.shared.datadirs import DataPaths
from twinraid.shared.logs import configure_logging, warn_if_mock
from twinraid.shared.mode import RunModeError, resolve_run_mode
from twinraid.shared.passwords import PasswordStore
from twinraid.worker.handler import CommandHandler
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
        load_config(runtime.config_path(env))
    except ConfigError as exc:
        logger.error("%s", exc.render())
        return EXIT_STARTUP_ERROR

    store = PasswordStore(DataPaths(runtime.data_dir(env)).password_file)
    password_check = PasswordCheck(store)
    handler = CommandHandler(build_status_provider(mode), password_check)
    server = WorkerServer(runtime.socket_path(env), allowed_uid, handler)
    try:
        asyncio.run(_serve(server, password_check))
    except SocketPathError as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR
    return 0


def _touch(path: Path) -> None:
    path.touch(mode=0o600)
    os.utime(path)


async def heartbeat(path: Path, interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """Aktualisiert regelmäßig eine Datei; der Docker-Healthcheck prüft ihr Alter."""
    while True:
        await asyncio.to_thread(_touch, path)
        await asyncio.sleep(interval)


async def _serve(server: WorkerServer, password_check: PasswordCheck) -> None:
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
    beat = asyncio.create_task(heartbeat(server.path.parent / HEARTBEAT_FILE))
    logger.info("worker listening on %s", server.path)
    try:
        await stop.wait()
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        await server.close()
        logger.info("worker stopped")
