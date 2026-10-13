"""Einheitliches Logging für alle TwinRaid-Prozesse.

Es wird nur nach stderr geloggt; im Container landet das im Container-Log.
Secrets dürfen niemals geloggt werden (CLAUDE.md, Regel 6).
"""

import logging
import os
import sys

from twinraid.shared.mode import RunMode

LOG_LEVEL_ENV_VAR = "TWINRAID_LOG_LEVEL"
_ALLOWED_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def configure_logging(process_name: str) -> None:
    """Richtet das Root-Logging mit dem Prozessnamen im Format ein."""
    level_name = os.environ.get(LOG_LEVEL_ENV_VAR, "INFO").upper()
    if level_name not in _ALLOWED_LEVELS:
        level_name = "INFO"
    logging.basicConfig(
        level=level_name,
        format=f"%(asctime)s %(levelname)-7s [{process_name}] %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def warn_if_mock(logger: logging.Logger, mode: RunMode) -> None:
    """Warnt beim Start deutlich, wenn der Simulationsmodus aktiv ist."""
    if mode is RunMode.MOCK:
        logger.warning(
            "SIMULATION MODE (TWINRAID_MODE=mock): no real servers are contacted, "
            "all status values and actions are simulated."
        )


def log_mock_banner(logger: logging.Logger, mode: RunMode) -> None:
    """Auffälliger mehrzeiliger Hinweis für die Startprüfung."""
    if mode is not RunMode.MOCK:
        return
    line = "=" * 72
    logger.warning(line)
    logger.warning("  SIMULATION MODE ACTIVE (TWINRAID_MODE=mock)")
    logger.warning("  No real servers are contacted. Status and actions are simulated.")
    logger.warning(line)
