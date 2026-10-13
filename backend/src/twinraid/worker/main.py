"""Startpunkt des Worker-Prozesses (twinraid-worker)."""

import asyncio
import logging
import os
import signal

from twinraid.config import ConfigError, load_config
from twinraid.integrations.factory import build_status_provider
from twinraid.shared.logs import configure_logging, warn_if_mock
from twinraid.shared.mode import RunModeError, resolve_run_mode
from twinraid.shared.runtime import RuntimeSettingError, config_path, socket_path, web_uid
from twinraid.worker.handler import CommandHandler
from twinraid.worker.server import SocketPathError, WorkerServer

logger = logging.getLogger("twinraid.worker")

EXIT_STARTUP_ERROR = 2


def main() -> int:
    # Der Worker legt nie Dateien an, die Gruppe oder andere lesen dürfen.
    os.umask(0o077)
    configure_logging("worker")
    env = os.environ
    try:
        mode = resolve_run_mode(env)
        allowed_uid = web_uid(env)
    except (RunModeError, RuntimeSettingError) as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR
    warn_if_mock(logger, mode)

    try:
        load_config(config_path(env))
    except ConfigError as exc:
        logger.error("%s", exc.render())
        return EXIT_STARTUP_ERROR

    handler = CommandHandler(build_status_provider(mode))
    server = WorkerServer(socket_path(env), allowed_uid, handler)
    try:
        asyncio.run(_serve(server))
    except SocketPathError as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR
    return 0


async def _serve(server: WorkerServer) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await server.start()
    logger.info("worker listening on %s", server.path)
    try:
        await stop.wait()
    finally:
        await server.close()
        logger.info("worker stopped")
