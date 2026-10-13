"""Startpunkt des Web-Prozesses (twinraid-web)."""

import logging
import os

import uvicorn

from twinraid.config import ConfigError, load_config
from twinraid.shared.logs import configure_logging, warn_if_mock
from twinraid.shared.mode import RunModeError, resolve_run_mode
from twinraid.shared.runtime import bind_host, config_path, frontend_dir, socket_path
from twinraid.web.app import WebSettings, create_app
from twinraid.web.auth import PlaceholderSessionVerifier
from twinraid.web.worker_client import WorkerClient

logger = logging.getLogger("twinraid.web")

EXIT_STARTUP_ERROR = 2


def main() -> int:
    configure_logging("web")
    env = os.environ
    try:
        mode = resolve_run_mode(env)
    except RunModeError as exc:
        logger.error("%s", exc)
        return EXIT_STARTUP_ERROR
    warn_if_mock(logger, mode)

    try:
        config = load_config(config_path(env))
    except ConfigError as exc:
        logger.error("%s", exc.render())
        return EXIT_STARTUP_ERROR

    settings = WebSettings(mode=mode, config=config, frontend_dir=frontend_dir(env))
    app = create_app(settings, WorkerClient(socket_path(env)), PlaceholderSessionVerifier(mode))
    logger.warning(
        "Login is not implemented yet (phase 1 placeholder). Only run this version locally."
    )
    uvicorn.run(
        app,
        host=bind_host(env),
        port=config.web.port,
        # X-Forwarded-For wird ignoriert (Konzept 5.1).
        proxy_headers=False,
        server_header=False,
        log_config=None,
    )
    return 0
