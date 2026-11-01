"""Startprüfung vor dem Start von Web-Prozess und Worker (twinraid-preflight).

Schlägt sie fehl, beendet sich der Container (Konzept 5.4 und 6.4).
"""

import logging
import os

from twinraid.config import ConfigError, load_config
from twinraid.shared.logs import configure_logging, log_mock_banner
from twinraid.shared.mode import RunModeError, resolve_run_mode
from twinraid.shared.runtime import config_path

logger = logging.getLogger("twinraid.preflight")

_NOT_STARTING = "TwinRaid will not start. Fix the problem above and restart the container."


def main() -> int:
    configure_logging("preflight")
    try:
        mode = resolve_run_mode(os.environ)
    except RunModeError as exc:
        logger.error("%s", exc)
        logger.error(_NOT_STARTING)
        return 1

    path = config_path(os.environ)
    try:
        config = load_config(path)
    except ConfigError as exc:
        logger.error("%s", exc.render())
        logger.error(_NOT_STARTING)
        return 1
    for warning in config.warnings():
        logger.warning("%s", warning)

    log_mock_banner(logger, mode)
    logger.info("preflight checks passed (mode=%s, config=%s)", mode.value, path)
    return 0
